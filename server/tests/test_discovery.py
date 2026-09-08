"""Synthetic discovery decisions exercise persistence without real model calls."""

import asyncio
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from radar.config import DiscoveryConfig, ProviderConfig, RadarConfig
from radar.db import database
from radar.discovery import DiscoveryService, queue_candidate, source_fingerprint
from radar.discovery_contracts import validate_decision
from radar.models import Article, DiscoveryCall, DiscoveryCandidate, now_iso
from radar.schemas import IncomingArticle


def item(uid="1", **changes):
    value = dict(platform="x", external_id=uid, url=f"https://x.com/synthetic/status/{uid}",
        title="Synthetic AI benchmark release", author="Synthetic researcher", handle="synthetic",
        text="We release an AI benchmark with reproducible tasks and a documented evaluation dataset. "
             "@testlab provides the repository and describes the limitations of this research.",
        published_at=datetime.now(UTC), metrics={"like_count": 1},
        entities=[dict(external_id="123", handle="testlab", name="Test Lab", description="AI research",
            followers_count=20, url="https://x.com/testlab", relation="mention", matched_text="@testlab")])
    value.update(changes)
    return IncomingArticle(**value)


def decision(uid, **changes):
    result = dict(candidate_id=uid, is_ai_relevant=True, novelty=80, specificity=85,
        potential_impact=80, confidence=78, should_surface=True,
        reason_zh="这是基于公开基准测试的潜力预判，仍需后续验证。", uncertainty_zh="目前缺少独立复现实验。",
        evidence_quotes=["reproducible tasks"], entities=[dict(external_id="123", kind="team",
            should_watch=True, confidence=90, reason_zh="原文明确提到团队提供研究代码。", evidence_quote="@testlab provides the repository")],
        named_entities=[])
    result.update(changes)
    return result


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.hook = None

    async def complete(self, prompt, schema):
        data = json.loads(prompt.split("\nUNTRUSTED_CANDIDATE:\n")[1])
        self.calls.append((prompt, schema))
        if self.hook:
            await self.hook(data)
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response(data) if callable(response) else response
        return json.dumps(decision(data["candidate_id"]), ensure_ascii=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    _, sessions = database(f"sqlite:///{tmp_path / 'discovery.db'}")
    config = RadarConfig(provider=ProviderConfig(kind="command"), discovery=DiscoveryConfig(enabled=True))
    model = FakeProvider()
    monkeypatch.setattr("radar.discovery.make_provider", lambda config: model)
    applied = []

    def publish(session, row):
        assert row.status == "accepted"
        assert row.result["candidate_id"] == row.id
        applied.append(row.id)
        row.published_article_id = row.article_key

    return sessions, config, model, applied, publish


def enqueue(env, value=None, **kwargs):
    sessions, config, *_ = env
    with sessions.begin() as session:
        row = queue_candidate(session, value or item(), config, **kwargs)
        return row.id if row else None


def service(env):
    return DiscoveryService(env[0], env[1], env[4])


def read(env, uid):
    with env[0]() as session:
        return session.get(DiscoveryCandidate, uid)


def test_metric_profile_changes_reuse_original_candidate_and_payload(env):
    first = item()
    uid = enqueue(env, first)
    changed = first.model_copy(deep=True)
    changed.metrics["like_count"] = 500
    changed.entities[0].followers_count = 10000
    changed.entities[0].description = "Changed profile biography"
    assert enqueue(env, changed) == uid
    row = read(env, uid)
    assert row.payload == first.model_dump(mode="json")
    assert row.latest_engagement == 500 and row.seed_qualified
    assert row.initial_engagement == 1


@pytest.mark.parametrize("field", ["title", "text", "author_external_id", "published_at"])
def test_semantic_source_changes_have_different_fingerprint(field):
    payload = item().model_dump(mode="json")
    changed = deepcopy(payload)
    changed[field] += "changed"
    assert source_fingerprint(payload) != source_fingerprint(changed)


def test_input_order_is_not_a_new_model_budget():
    payload = item().model_dump(mode="json")
    payload["references"] = [{"url": "https://example.org/a"}, {"url": "https://example.org/b"}]
    other = deepcopy(payload)
    other["references"].reverse()
    assert source_fingerprint(payload) == source_fingerprint(other)


@pytest.mark.parametrize("changes,priority,expected", [
    ({"text": "AI is amazing!", "entities": []}, False, False),
    ({"text": "AI is amazing!", "entities": []}, True, False),
    ({"text": "AI announcement by @testlab"}, True, True),
    ({"text": "AI announcement by @testlab"}, False, False),
    ({"text": "New AI model release with a documented source: https://example.org/code", "entities": []}, False, True),
    ({"published_at": datetime.now(UTC) - timedelta(hours=49)}, True, False),
    ({"published_at": datetime.now(UTC) + timedelta(hours=1)}, True, False),
    ({"title": "Garden", "text": "Flowers in the garden " * 8, "entities": []}, False, False),
    ({"platform": "web", "entities": []}, False, False),
])
def test_candidate_gate_is_bounded_and_requires_evidence(env, changes, priority, expected):
    assert bool(enqueue(env, item(**changes), source_priority=priority)) is expected


def test_candidate_budget_preserves_existing_state_and_reports_capacity(env):
    env[1].discovery.max_candidates_per_day = 1
    uid = enqueue(env)
    assert enqueue(env, item("2")) is None
    assert read(env, uid).status == "pending"
    assert service(env).status()["candidate_limit_reached"]


@pytest.mark.asyncio
async def test_success_has_pre_call_reservation_and_restart_uses_saved_result(env):
    uid = enqueue(env)

    async def hook(data):
        with env[0]() as session:
            row = session.get(DiscoveryCandidate, uid)
            call = session.scalar(select(DiscoveryCall))
            assert row.status == call.status == "reserved"
            assert row.owner == call.owner and row.fingerprint == call.fingerprint
            assert call.provider["request_upper_bound"] is None
            assert session.scalar(select(func.count()).select_from(Article)) == 0

    env[2].hook = hook
    result = await service(env).pending()
    assert result == dict(processed=1, judged=1, applied=1, failed=0, more_pending=False)
    assert read(env, uid).status == "done"
    snapshot = read(env, uid).payload
    await service(env).pending()
    assert len(env[2].calls) == 1 and env[3] == [uid]
    assert read(env, uid).payload == snapshot


@pytest.mark.asyncio
async def test_negative_decision_is_saved_not_rejudged(env):
    uid = enqueue(env)
    env[2].responses = [lambda data: json.dumps(decision(data["candidate_id"], should_surface=False, is_ai_relevant=False))]
    await service(env).pending()
    await service(env).pending()
    assert not read(env, uid).result["should_surface"]
    assert len(env[2].calls) == 1 and read(env, uid).status == "done"


@pytest.mark.asyncio
async def test_completed_result_survives_apply_failure_without_new_model(env):
    uid = enqueue(env)

    def fail(session, row):
        row.published_article_id = "rollback-this"
        raise RuntimeError("private secret")

    svc = DiscoveryService(env[0], env[1], fail)
    await svc.pending()
    row = read(env, uid)
    assert row.status == "accepted" and row.result and not row.published_article_id
    assert row.error_code == "apply_failed" and not svc.has_pending()
    with env[0].begin() as session:
        session.get(DiscoveryCandidate, uid).retry_at = ""
    env[1].discovery.max_calls_per_day = 1
    assert (await service(env).pending())["applied"] == 1
    assert len(env[2].calls) == 1


@pytest.mark.asyncio
async def test_source_edit_supersedes_pending_and_completed_unapplied(env):
    first = item()
    uid = enqueue(env, first)
    new = first.model_copy(update={"text": first.text + " Updated evidence."})
    second = enqueue(env, new)
    assert read(env, uid).status == "superseded"
    await service(env).pending()
    assert env[3] == [second]


@pytest.mark.asyncio
async def test_source_edit_during_request_saves_receipt_but_never_applies_old(env):
    first = item()
    uid = enqueue(env, first)

    async def hook(data):
        enqueue(env, first.model_copy(update={"text": "AI changed", "entities": []}))

    env[2].hook = hook
    await service(env).pending()
    assert read(env, uid).status == "superseded" and env[3] == []
    with env[0]() as session:
        assert session.scalar(select(DiscoveryCall)).status == "completed"


@pytest.mark.parametrize("exception", [TimeoutError("secret"), RuntimeError("private CLI error"), ConnectionError("secret")])
@pytest.mark.asyncio
async def test_uncertain_failure_cannot_be_replayed(env, exception):
    uid = enqueue(env)
    env[2].responses = [exception]
    await service(env).pending()
    assert read(env, uid).status == "unknown"
    assert read(env, uid).error_code == "outcome_unknown"
    assert not service(env).has_pending()
    service(env).recover()
    await service(env).pending()
    assert len(env[2].calls) == 1


@pytest.mark.asyncio
async def test_cancelled_call_is_unknown_and_cancellation_propagates(env):
    uid = enqueue(env)
    env[2].responses = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await service(env).pending()
    assert read(env, uid).status == "unknown" and not service(env).has_pending()


def test_recover_expired_reservation_is_offline_and_nonreplayable(env):
    uid = enqueue(env)
    reserved = service(env)._reserve(uid)
    assert reserved
    assert service(env).recover() == 0
    with env[0].begin() as session:
        session.get(DiscoveryCandidate, uid).lease_until = "2000-01-01T00:00:00+00:00"
    assert service(env).recover() == 1
    assert not service(env).has_pending()
    assert len(env[2].calls) == 0
    with env[0]() as session:
        assert session.scalar(select(DiscoveryCall)).status == "unknown"


def test_stale_owner_cannot_store_or_fail_after_recovery(env):
    uid = enqueue(env)
    reserved = service(env)._reserve(uid)
    _, owner, call_id, payload, _ = reserved
    with env[0].begin() as session:
        session.get(DiscoveryCandidate, uid).lease_until = "2000-01-01T00:00:00+00:00"
    service(env).recover()
    parsed = validate_decision(json.dumps(decision(uid)), uid, payload)
    assert not service(env)._save(uid, owner, call_id, parsed)
    service(env)._failure(uid, owner, call_id, RuntimeError("secret"))
    assert read(env, uid).status == "unknown" and not read(env, uid).result


def http_error(code):
    response = httpx.Response(code, request=httpx.Request("POST", "https://example.invalid"))
    return httpx.HTTPStatusError("do not expose private body", request=response.request, response=response)


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.asyncio
async def test_known_failure_retries_at_most_twice_with_durable_backoff(env, status):
    uid = enqueue(env)
    env[2].responses = [http_error(status), http_error(status)]
    await service(env).pending()
    assert read(env, uid).status == "retry_wait" and not service(env).has_pending()
    await service(env).pending()
    assert len(env[2].calls) == 1
    with env[0].begin() as session:
        session.get(DiscoveryCandidate, uid).retry_at = ""
    await service(env).pending()
    assert read(env, uid).status == "needs_attention"
    assert read(env, uid).error_code == "call_budget_exhausted"
    assert not service(env).has_pending() and len(env[2].calls) == 2


@pytest.mark.parametrize("code,expected", [(402, "insufficient_balance"), (401, "auth_required"), (403, "auth_required")])
@pytest.mark.asyncio
async def test_auth_balance_errors_are_visible_without_automatic_spend(env, code, expected):
    uid = enqueue(env)
    env[2].responses = [http_error(code)]
    await service(env).pending()
    assert read(env, uid).error_code == expected and not service(env).has_pending()


@pytest.mark.asyncio
async def test_daily_budget_and_batch_limit_do_not_starve_after_unknown(env):
    first = enqueue(env, item("1"))
    enqueue(env, item("2"), source_priority=True)
    enqueue(env, item("3"))
    env[1].discovery.max_calls_per_day = 2
    env[2].responses = [RuntimeError("unknown")]
    assert (await service(env).pending(limit=100))["processed"] == 2
    assert read(env, first).status == "unknown" and len(env[2].calls) == 2
    assert not service(env).has_pending()
    with env[0].begin() as session:
        for call in session.scalars(select(DiscoveryCall)):
            call.created_at = "2000-01-01T00:00:00+00:00"
    await service(env).pending()
    assert len(env[2].calls) == 3 and read(env, first).status == "unknown"


@pytest.mark.asyncio
async def test_concurrent_workers_only_reserve_once(env):
    enqueue(env)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def hook(data):
        entered.set()
        await finish.wait()

    env[2].hook = hook
    task = asyncio.create_task(service(env).pending())
    await asyncio.wait_for(entered.wait(), 2)
    assert not service(env).has_pending()
    assert (await service(env).pending())["processed"] == 0
    finish.set()
    await asyncio.wait_for(task, 2)
    assert len(env[2].calls) == 1


@pytest.mark.asyncio
async def test_extractive_is_not_a_model_judgment_and_get_does_not_build_provider(env, monkeypatch):
    uid = enqueue(env)
    env[1].provider.kind = "extractive"

    def prohibited(*args):
        raise AssertionError("GET or unsupported provider constructed model")

    monkeypatch.setattr("radar.discovery.make_provider", prohibited)
    assert service(env).status()["counts"] == {"pending": 1}
    assert service(env).has_pending()
    await service(env).pending()
    assert read(env, uid).error_code == "provider_unsupported"
    assert service(env).status()["calls_today"] == 0


@pytest.mark.parametrize("change", [
    {"candidate_id": "0" * 64}, {"novelty": True}, {"should_surface": 1},
    {"confidence": 101}, {"title": "A replacement title"}, {"evidence_quotes": ["invented source"]},
    {"reason_zh": "This is a promising research release"}, {"uncertainty_zh": ""},
    {"evidence_quotes": []}, {"reason_zh": "这一定会获得成功。"},
    {"entities": [dict(external_id="999", kind="person", should_watch=True, confidence=90,
        reason_zh="值得跟进这个账号。", evidence_quote="@testlab provides the repository")]},
    {"entities": [dict(external_id="123", kind="person", should_watch=True, confidence=90,
        reason_zh="值得跟进这个账号。", evidence_quote="reproducible tasks")]},
    {"named_entities": [dict(name="Imaginary Lab", kind="team", reason_zh="值得跟进这个团队。", evidence_quote="reproducible tasks")]},
])
@pytest.mark.asyncio
async def test_invalid_result_never_publishes_or_retries(env, change):
    uid = enqueue(env)
    env[2].responses = [json.dumps(decision(uid, **change))]
    await service(env).pending()
    assert read(env, uid).status == "needs_attention" and read(env, uid).error_code == "format_invalid"
    await service(env).pending()
    assert len(env[2].calls) == 1 and not env[3]


def test_profile_biography_and_partial_handle_cannot_be_account_evidence():
    payload = item(text="AI research from @testlab2. " + "Detailed research methods. " * 5).model_dump(mode="json")
    payload["entities"][0]["matched_text"] = None
    value = decision("0" * 64, evidence_quotes=["Detailed research methods."])
    value["entities"][0]["evidence_quote"] = "@testlab2"
    with pytest.raises(ValueError):
        validate_decision(json.dumps(value), "0" * 64, payload)


def test_expired_pending_is_retained_but_frees_capacity(env):
    env[1].discovery.max_pending = 1
    uid = enqueue(env)
    with env[0].begin() as session:
        row = session.get(DiscoveryCandidate, uid)
        row.payload = {**row.payload, "published_at": "2000-01-01T00:00:00+00:00"}
    assert enqueue(env, item("2"))
    assert read(env, uid).status == "expired"


def test_known_reservation_retains_provider_request_upper_bound(env):
    env[1].provider.kind = "openai_chat"
    uid = enqueue(env)
    service(env)._reserve(uid)
    with env[0]() as session:
        call = session.scalar(select(DiscoveryCall))
        assert call.provider["request_upper_bound"] == 3
        assert call.created_at <= now_iso()


@pytest.mark.asyncio
async def test_real_process_exit_after_reservation_never_replays(env):
    uid = enqueue(env)
    db_url = str(env[0].kw["bind"].url)
    script = """
import asyncio, os, sys
from radar.config import RadarConfig, DiscoveryConfig, ProviderConfig
from radar.db import database
from radar.discovery import DiscoveryService
import radar.discovery as module
class ExitProvider:
    async def complete(self, prompt, schema):
        os._exit(77)
_, sessions = database(sys.argv[1])
config = RadarConfig(provider=ProviderConfig(kind='command'), discovery=DiscoveryConfig(enabled=True))
module.make_provider = lambda config: ExitProvider()
asyncio.run(DiscoveryService(sessions, config, lambda session, row: None).pending())
"""
    result = subprocess.run([sys.executable, "-c", script, db_url], env=os.environ.copy(),
                            capture_output=True, timeout=10)
    assert result.returncode == 77, result.stderr.decode()
    assert read(env, uid).status == "reserved" and not read(env, uid).result
    with env[0].begin() as session:
        session.get(DiscoveryCandidate, uid).lease_until = "2000-01-01T00:00:00+00:00"
    assert service(env).recover() == 1
    await service(env).pending()
    assert len(env[2].calls) == 0 and read(env, uid).status == "unknown"


@pytest.mark.asyncio
async def test_apply_cannot_rewrite_immutable_result_or_call_receipt(env):
    uid = enqueue(env)

    def bad_apply(session, row):
        row.result = {**row.result, "should_surface": False}

    result = await DiscoveryService(env[0], env[1], bad_apply).pending()
    assert result["failed"] == 1 and result["applied"] == 0
    row = read(env, uid)
    assert row.result["should_surface"] and row.status == "accepted"
    with env[0]() as session:
        assert session.scalar(select(DiscoveryCall)).result == row.result


def test_non_ai_new_version_invalidates_old_pending_without_new_candidate(env):
    original = item()
    uid = enqueue(env, original)
    changed = original.model_copy(update={"title": "Gardening", "text": "Flowers and trees in spring", "entities": []})
    assert enqueue(env, changed) is None
    assert read(env, uid).status == "superseded"


@pytest.mark.asyncio
async def test_known_retry_can_complete_and_clear_fixed_error(env):
    uid = enqueue(env)
    env[2].responses = [http_error(429)]
    await service(env).pending()
    assert service(env).status()["errors"] == {"rate_limited": 1}
    with env[0].begin() as session:
        session.get(DiscoveryCandidate, uid).retry_at = ""
    await service(env).pending()
    assert read(env, uid).status == "done" and service(env).status()["errors"] == {}
    assert len(env[2].calls) == 2


@pytest.mark.asyncio
async def test_seed_first_keeps_half_of_default_candidates_and_calls_for_low(env):
    for index in range(30):
        assert enqueue(env, item(f"seed{index}"), source_priority=True)
    assert enqueue(env, item("seedoverflow"), source_priority=True) is None
    low = enqueue(env, item("low"))
    assert low
    # Earlier seed rows may use their ten calls, but cannot use low's ten calls.
    for _ in range(5):
        await service(env).pending()
    pools = service(env).status()["pools"]
    assert pools["seed"]["candidate_limit"] == pools["low_engagement"]["candidate_limit"] == 30
    assert pools["seed"]["call_limit"] == pools["low_engagement"]["call_limit"] == 10
    assert pools["seed"]["calls_today"] == 10
    assert service(env).has_pending()
    await service(env).pending()
    assert read(env, low).status == "done"
    assert service(env).status()["pools"]["low_engagement"]["calls_today"] == 1


@pytest.mark.asyncio
async def test_exhausted_low_calls_cannot_borrow_seed_share(env):
    env[1].discovery.max_calls_per_day = 4
    for index in range(3):
        assert enqueue(env, item(f"low{index}"))
    seed = enqueue(env, item("seed"), source_priority=True)
    await service(env).pending()
    assert service(env).status()["pools"]["low_engagement"]["call_limit_reached"]
    await service(env).pending()
    assert read(env, seed).status == "done"
    assert len(env[2].calls) == 3
    assert service(env).status()["pools"]["low_engagement"]["calls_today"] == 2
    assert not service(env).has_pending()


@pytest.mark.parametrize("first_low", [False, True])
def test_odd_candidate_budget_is_reserved_bidirectionally(env, first_low):
    env[1].discovery.max_candidates_per_day = 5
    for low in (first_low, not first_low):
        cap = 3 if low else 2
        for index in range(cap):
            assert enqueue(env, item(f"{low}-{index}"), source_priority=not low)
        assert enqueue(env, item(f"{low}-overflow"), source_priority=not low) is None
    pools = service(env).status()["pools"]
    assert pools["low_engagement"]["candidates_today"] == pools["low_engagement"]["candidate_limit"] == 3
    assert pools["seed"]["candidates_today"] == pools["seed"]["candidate_limit"] == 2


@pytest.mark.asyncio
async def test_odd_call_budget_favors_low_without_exceeding_total(env):
    env[1].discovery.max_calls_per_day = 5
    for index in range(3):
        enqueue(env, item(f"low{index}"))
    for index in range(2):
        enqueue(env, item(f"seed{index}"), source_priority=True)
    for _ in range(4):
        await service(env).pending()
    status = service(env).status()
    assert status["calls_today"] == 5
    assert status["pools"]["low_engagement"]["calls_today"] == 3
    assert status["pools"]["seed"]["calls_today"] == 2


@pytest.mark.parametrize("initial_low", [False, True])
def test_article_keeps_first_pool_across_metrics_priority_and_content_changes(env, initial_low):
    first = item()
    uid = enqueue(env, first, source_priority=not initial_low)
    changed = first.model_copy(update={"metrics": {"like_count": 999 if initial_low else 0}})
    assert enqueue(env, changed, source_priority=initial_low) == uid
    assert read(env, uid).low_engagement is initial_low
    next_version = enqueue(env, changed.model_copy(update={"text": changed.text + " New details."}),
                           source_priority=initial_low)
    assert read(env, next_version).low_engagement is initial_low
    assert read(env, uid).status == "superseded"

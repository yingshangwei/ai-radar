"""Real SQLite/coordinator checks using only synthetic, inert transport receipts."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from radar import codex_sessions
from radar.config import RadarConfig
from radar.db import database
from radar.discovery import DiscoveryService, queue_candidate
from radar.discovery_contracts import DiscoveryBatchDecision
from radar.discovery_sessions import DiscoverySessions
from radar.models import AgentBatch, AgentSession, DiscoveryCall, DiscoveryCandidate
from radar.schemas import IncomingArticle


def outcome(candidate):
    return {"candidate_id": candidate["candidate_id"], "is_ai_relevant": True,
        "novelty": 70, "specificity": 80, "potential_impact": 75, "confidence": 78,
        "should_surface": True, "reason_zh": "这是根据当前原文作出的潜力预判，仍需验证。",
        "uncertainty_zh": "目前缺少独立复现实验。",
        "evidence_quotes": [candidate["text"].split(".")[0]], "entities": [], "named_entities": []}


class FakeTransport:
    def __init__(self):
        self.calls, self.receipts = [], {}
        self.hook, self.modify, self.error = None, None, None

    async def run(self, config, workdir, prompt, schema_type, *, session_id=None, on_thread=None, lock_fd=None):
        assert schema_type is DiscoveryBatchDecision
        assert lock_fd is not None
        batch = json.loads(prompt.split("\nCURRENT_BATCH:\n")[1])
        thread = session_id or str(uuid4())
        self.calls.append({"batch": deepcopy(batch), "session_id": session_id, "thread": thread})
        if on_thread:
            await on_thread(thread)
        if self.hook:
            await self.hook(batch)
        if self.error:
            error, self.error = self.error, None
            raise error
        payload = {"batch_id": batch["batch_id"], "decisions": [outcome(c) for c in batch["candidates"]]}
        if self.modify:
            self.modify(payload, batch)
        receipt = codex_sessions.TransportResult(session_id=thread, text=json.dumps(payload),
            turn_id=str(uuid4()), workdir=str(workdir), event_path=str(Path(workdir) / "events.jsonl"),
            result_path=str(Path(workdir) / "result.json"),
            request_fingerprint=codex_sessions.request_fingerprint(config, prompt, schema_type, session_id),
            usage={"input_tokens": 100, "output_tokens": 50})
        self.receipts[str(workdir)] = [receipt]
        return receipt

    def scan(self, workdir, schema_type=None):
        assert schema_type is DiscoveryBatchDecision
        return list(self.receipts.get(str(workdir), []))


@pytest.fixture
def batches(tmp_path, monkeypatch):
    engine, sessions = database(f"sqlite:///{tmp_path}/batch.db")
    config = RadarConfig(provider={"kind": "codex", "model": "synthetic-model"},
        discovery={"enabled": True, "session_reuse": True, "batch_size": 2,
            "session_directory": str(tmp_path / "private-sessions")})
    transport = FakeTransport()
    monkeypatch.setattr(codex_sessions, "run", transport.run)
    monkeypatch.setattr(codex_sessions, "scan_artifacts", transport.scan)

    def no_legacy(*_args, **_kwargs):
        raise AssertionError("Persistent discovery invoked a separate one-shot model")

    monkeypatch.setattr("radar.discovery.make_provider", no_legacy)
    applied = []

    def publish(session, row):
        assert row.status == "accepted" and row.result["candidate_id"] == row.id
        applied.append(row.id)
        row.published_article_id = row.article_key

    service = DiscoveryService(sessions, config, publish)
    yield service, sessions, config, transport, applied
    engine.dispose()


def add(batches, uid, *, seed=False):
    service, sessions, config, *_ = batches
    value = IncomingArticle(platform="x", external_id=str(uid), url=f"https://x.com/fixture/status/{uid}",
        author="Synthetic researcher", handle="fixture", title=f"AI research fixture {uid}",
        text=f"AI research fixture {uid} releases a unique reproducible evaluation dataset. "
             "We document the benchmark methodology and the limitations of the research results.",
        published_at=datetime.now(UTC), metrics={"like_count": 200 if seed else 1})
    with sessions.begin() as session:
        candidate = queue_candidate(session, value, config, source_priority=seed)
        assert candidate
        return candidate.id


def rows(sessions, model):
    with sessions() as session:
        return session.scalars(select(model).order_by(model.created_at, model.id)).all()


def records(sessions, model):
    return [{column.name: deepcopy(getattr(row, column.name)) for column in model.__table__.columns}
        for row in rows(sessions, model)]


async def test_two_multi_candidate_batches_resume_exact_session_and_keep_every_receipt(batches):
    service, sessions, _, transport, applied = batches
    keys = [add(batches, index) for index in range(4)]
    assert (await service.pending())["judged"] == 2
    replacement = DiscoveryService(sessions, service.config, service.publish_callback)
    assert (await replacement.pending())["judged"] == 2
    assert transport.calls[0]["session_id"] is None
    assert transport.calls[1]["session_id"] == transport.calls[0]["thread"]
    assert [[c["candidate_id"] for c in call["batch"]["candidates"]] for call in transport.calls] == [keys[:2], keys[2:]]
    assert applied == keys
    assert len(rows(sessions, AgentSession)) == 1
    assert rows(sessions, AgentSession)[0].turns == 2
    assert len(rows(sessions, AgentBatch)) == 2
    assert len(rows(sessions, DiscoveryCall)) == 4
    assert all(row.status == "done" for row in rows(sessions, DiscoveryCandidate))
    before = records(sessions, DiscoveryCall)
    assert (await replacement.pending())["processed"] == 0
    assert len(transport.calls) == 2 and records(sessions, DiscoveryCall) == before


async def test_concurrent_services_serialize_on_database_lock_and_preserve_fifo(batches):
    service, sessions, _, transport, applied = batches
    keys = [add(batches, index) for index in range(4)]
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(batch):
        entered.set()
        await release.wait()

    transport.hook = block
    active = asyncio.create_task(service.pending())
    await asyncio.wait_for(entered.wait(), 2)
    other = DiscoveryService(sessions, service.config, service.publish_callback)
    try:
        result = await other.pending()
        assert result["processed"] == 0 and result["more_pending"]
        assert len(transport.calls) == 1
        assert len(rows(sessions, DiscoveryCall)) == 2
    finally:
        release.set()
        await asyncio.wait_for(active, 2)
    transport.hook = None
    assert (await other.pending())["judged"] == 2
    assert applied == keys
    assert transport.calls[1]["session_id"] == transport.calls[0]["thread"]


async def test_daily_and_pool_allowances_count_candidates_not_transport_turns(batches):
    service, sessions, config, transport, _ = batches
    config.discovery.batch_size = 4
    config.discovery.max_calls_per_day = 4
    low = [add(batches, index) for index in range(3)]
    seeds = [add(batches, index, seed=True) for index in range(3, 6)]

    async def check_reserved(batch):
        report = service.status()
        assert report["calls_today"] == 4
        assert report["pools"]["low_engagement"]["calls_today"] == 2
        assert report["pools"]["seed"]["calls_today"] == 2

    transport.hook = check_reserved
    assert (await service.pending())["judged"] == 4
    selected = [candidate["candidate_id"] for candidate in transport.calls[0]["batch"]["candidates"]]
    assert selected == low[:2] + seeds[:2]
    assert not service.has_pending()
    assert (await service.pending())["processed"] == 0
    assert len(transport.calls) == 1
    # Only synthetic accounting timestamps are advanced to represent yesterday's spent units.
    with sessions.begin() as session:
        for call in session.scalars(select(DiscoveryCall)):
            call.created_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    transport.hook = None
    replacement = DiscoveryService(sessions, config, service.publish_callback)
    assert (await replacement.pending())["judged"] == 2
    assert [c["candidate_id"] for c in transport.calls[1]["batch"]["candidates"]] == [low[2], seeds[2]]
    assert len(rows(sessions, DiscoveryCall)) == 6


@pytest.mark.parametrize("invalid", ["batch_id", "missing", "duplicate", "unknown_id", "foreign_quote"])
async def test_invalid_batch_is_held_as_a_whole_and_never_replayed(batches, invalid):
    service, sessions, _, transport, applied = batches
    keys = [add(batches, index) for index in range(2)]

    def alter(payload, batch):
        if invalid == "batch_id":
            payload["batch_id"] = str(uuid4())
        elif invalid == "missing":
            payload["decisions"].pop()
        elif invalid == "duplicate":
            payload["decisions"][1] = deepcopy(payload["decisions"][0])
        elif invalid == "unknown_id":
            payload["decisions"][1]["candidate_id"] = "f" * 64
        else:
            payload["decisions"][0]["evidence_quotes"] = [batch["candidates"][1]["text"].split(".")[0]]

    transport.modify = alter
    result = await service.pending()
    assert result["judged"] == result["applied"] == 0 and result["failed"] == 2
    assert not applied
    candidates = rows(sessions, DiscoveryCandidate)
    assert {row.id for row in candidates} == set(keys)
    assert all(row.status == "needs_attention" and row.error_code == "format_invalid" and not row.result for row in candidates)
    assert all(row.status == "failed" for row in rows(sessions, DiscoveryCall))
    assert rows(sessions, AgentSession)[0].status == "closed"
    replacement = DiscoveryService(sessions, service.config, service.publish_callback)
    assert replacement.recover() == 0
    assert (await replacement.pending())["processed"] == 0
    assert len(transport.calls) == 1
    rejected_receipts = records(sessions, DiscoveryCall)
    transport.modify = None
    fresh = [add(batches, index) for index in range(2, 4)]
    assert (await replacement.pending())["judged"] == 2
    assert applied == fresh and len(transport.calls) == 2
    assert transport.calls[1]["session_id"] is None
    assert records(sessions, DiscoveryCall)[:2] == rejected_receipts


async def test_completed_artifact_recovers_after_database_commit_window_without_new_run(batches, monkeypatch):
    service, sessions, config, transport, applied = batches
    keys = [add(batches, index) for index in range(2)]
    original = DiscoverySessions._finish

    def crash(*_args):
        raise SystemExit(77)

    monkeypatch.setattr(DiscoverySessions, "_finish", crash)
    with pytest.raises(SystemExit):
        await service.pending()
    assert len(transport.calls) == 1 and transport.receipts and not applied
    assert all(row.status == "reserved" for row in rows(sessions, DiscoveryCandidate))
    monkeypatch.setattr(DiscoverySessions, "_finish", original)
    replacement = DiscoveryService(sessions, config, service.publish_callback)
    assert replacement.has_pending()
    result = await replacement.pending()
    assert result["applied"] == 2 and len(transport.calls) == 1
    assert applied == keys
    assert all(row.status == "completed" for row in rows(sessions, DiscoveryCall))
    assert replacement.recover() == 0 and not replacement.has_pending()


async def test_unknown_batch_closes_session_but_does_not_starve_new_candidates(batches):
    service, sessions, config, transport, applied = batches
    unknown = [add(batches, index) for index in range(2)]
    transport.error = codex_sessions.CodexSessionUnknown("synthetic_interrupted_turn")
    assert (await service.pending())["failed"] == 2
    first_thread = transport.calls[0]["thread"]
    before = records(sessions, DiscoveryCall)
    fresh = [add(batches, index) for index in range(2, 4)]
    replacement = DiscoveryService(sessions, config, service.publish_callback)
    assert (await replacement.pending())["judged"] == 2
    assert applied == fresh and len(transport.calls) == 2
    assert transport.calls[1]["session_id"] is None and transport.calls[1]["thread"] != first_thread
    assert records(sessions, DiscoveryCall)[:2] == before
    assert {row.id for row in rows(sessions, DiscoveryCandidate) if row.status == "unknown"} == set(unknown)
    assert service.status()["calls_today"] == 4


@pytest.mark.parametrize("rotation", ["turn_limit", "configuration_changed", "age_limit", "policy_changed"])
async def test_rotation_creates_fresh_session_without_resetting_budget_or_decisions(batches, rotation):
    service, sessions, config, transport, _ = batches
    add(batches, "1")
    await service.pending()
    before = records(sessions, DiscoveryCall)
    if rotation == "turn_limit":
        config.discovery.session_max_turns = 1
    elif rotation == "configuration_changed":
        config.provider.model = "different-synthetic-model"
    elif rotation == "policy_changed":
        config.discovery.policy = "synthetic-new-policy"
    else:
        with sessions.begin() as session:
            session.scalar(select(AgentSession)).created_at = (datetime.now(UTC) - timedelta(
                hours=config.discovery.session_max_age_hours + 1)).isoformat()
    add(batches, "2")
    replacement = DiscoveryService(sessions, config, service.publish_callback)
    assert (await replacement.pending())["judged"] == 1
    assert transport.calls[1]["session_id"] is None
    assert transport.calls[1]["thread"] != transport.calls[0]["thread"]
    assert records(sessions, DiscoveryCall)[:1] == before
    assert replacement.status()["calls_today"] == 2
    conversations = rows(sessions, AgentSession)
    assert conversations[0].status == "closed"
    expected = "configuration_changed" if rotation == "policy_changed" else rotation
    assert conversations[0].close_reason == expected


async def test_apply_failure_resumes_saved_batch_result_after_backoff_even_with_no_budget(batches):
    service, sessions, config, transport, applied = batches
    key = add(batches, "1")
    original = service.publish_callback

    def reject(session, row):
        row.published_article_id = "must-rollback"
        raise RuntimeError("synthetic storage failure")

    service.publish_callback = reject
    result = await service.pending()
    assert result["judged"] == result["failed"] == 1 and result["applied"] == 0
    before = records(sessions, DiscoveryCall)
    assert not service.has_pending()
    with sessions.begin() as session:
        row = session.get(DiscoveryCandidate, key)
        assert row.status == "accepted" and row.error_code == "apply_failed" and not row.published_article_id
        row.retry_at = ""
    config.discovery.max_calls_per_day = 1
    replacement = DiscoveryService(sessions, config, original)
    assert replacement.has_pending()
    assert (await replacement.pending())["applied"] == 1
    assert applied == [key] and len(transport.calls) == 1
    assert records(sessions, DiscoveryCall) == before
    assert not replacement.has_pending()


async def test_discovery_never_borrows_a_factual_audit_session(batches):
    service, sessions, _, transport, _ = batches
    with sessions.begin() as session:
        session.add(AgentSession(scope="summary-audit", configuration="same-role-is-required", cli_session_id=str(uuid4())))
    audit_before = records(sessions, AgentSession)[0]
    add(batches, "1")
    assert (await service.pending())["judged"] == 1
    assert transport.calls[0]["session_id"] is None
    audit_after = next(row for row in records(sessions, AgentSession) if row["scope"] == "summary-audit")
    assert audit_after == audit_before

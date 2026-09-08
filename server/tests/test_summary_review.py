"""Persistent review recovery uses only synthetic evidence and fake model transport."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from radar.config import ProviderConfig, RadarConfig, SummaryReviewConfig
from radar.db import database
from radar.models import SummaryReview, now_iso
from radar.summary_review import FORMAT_FEEDBACK, SummaryReviewPending, SummaryReviewService


def sources(*ids):
    return [{"id": uid, "title": "Synthetic model", "text": "The synthetic model supports five tools.",
             "author": "Synthetic researcher", "url": f"https://example.org/{uid}"} for uid in ids or ("a",)]


def candidate(uid):
    return {"source_id": uid, "title_zh": "合成模型工具支持", "summary_zh": "研究者介绍了模型支持的工具。",
            "key_points_zh": ["这些工具来自合成测试材料。"], "why_it_matters_zh": "这可能扩展模型的适用场景。"}


class Model:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.reject_ids = set()
        self.unchanged = False
        self.forever_reject = False
        self.hook = None

    async def complete(self, prompt, schema):
        name = schema.__name__
        self.calls.append((name, prompt))
        if self.hook:
            await self.hook(name)
        if self.responses.get(name):
            value = self.responses[name].pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        if name == "ReadingOutput":
            inputs = json.loads(prompt.split("\nUNTRUSTED_DOCUMENTS:\n")[1])
            value = {"documents": [candidate(row["id"]) for row in inputs]}
        elif name == "DigestOutput":
            inputs = json.loads(prompt.split("\nUNTRUSTED_SOURCE_DATA:\n")[1])["articles"]
            value = {"title": "合成模型研究进展", "overview": "研究者介绍了模型工具支持。",
                     "stories": [{"title": "合成模型研究", "summary": "研究者介绍了合成工具支持。",
                                  "why_it_matters": "这可能扩展适用场景。", "category": "模型",
                                  "source_ids": [item["id"]]} for item in inputs]}
        else:
            units = json.loads(prompt.split("\nUNTRUSTED_REVIEW_INPUT:\n")[1])["units"]
            if name == "SummaryAuditOutput":
                value = {"audits": []}
                for unit in units:
                    reject = bool(set(unit["source_ids"]) & self.reject_ids) and (
                        self.forever_reject or "修订" not in json.dumps(unit["candidate"], ensure_ascii=False))
                    field = {"header": "overview", "story": "summary", "document": "summary_zh"}[unit["kind"]]
                    value["audits"].append({"unit_id": unit["unit_id"], "approved": not reject,
                        "issues": [{"field": field, "reason": "候选事实需要核实。", "source_ids": unit["source_ids"]}] if reject else []})
            else:
                assert name == "SummaryCorrections"
                value = {"corrections": []}
                for unit in units:
                    replacement = deepcopy(unit["candidate"])
                    if not self.unchanged:
                        field = {"header": "overview", "story": "summary", "document": "summary_zh"}[unit["kind"]]
                        replacement[field] += "修订后保留来源限定。"
                    value["corrections"].append({"unit_id": unit["unit_id"], "candidate": replacement})
        return json.dumps(value, ensure_ascii=False)

    def count(self, name):
        return sum(stage == name for stage, _ in self.calls)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    engine, sessions = database(f"sqlite:///{tmp_path / 'review.db'}")
    model = Model()
    config = RadarConfig(provider=ProviderConfig(kind="command"))
    monkeypatch.setattr("radar.summary_review.make_provider", lambda _: model)
    yield sessions, config, model
    engine.dispose()


def saved(sessions):
    with sessions() as session:
        rows = session.scalars(select(SummaryReview)).all()
        assert len(rows) == 1
        return {column.name: deepcopy(getattr(rows[0], column.name)) for column in SummaryReview.__table__.columns}


def due(sessions):
    # Synthetic test database clock control; never changes production evidence.
    with sessions.begin() as session:
        row = session.scalar(select(SummaryReview))
        row.retry_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()


@pytest.mark.asyncio
async def test_exact_approval_reuses_across_restart_and_ignores_generator_metadata(harness):
    sessions, config, model = harness
    initial = sources()
    service = SummaryReviewService(sessions, config)
    result = await service.analyze_documents("document:a", initial, model, evidence=sources())
    before = saved(sessions)
    changed = sources()
    changed[0].update(score=999, title_zh="不同派生标题", text_zh="不同派生译文")
    restarted = SummaryReviewService(sessions, config)
    assert restarted.can_analyze_documents("document:a", changed, evidence=sources())
    again = await restarted.analyze_documents("document:a", changed, model, evidence=sources())
    assert again == result and model.count("ReadingOutput") == 1 and model.count("SummaryAuditOutput") == 1
    assert saved(sessions) == before
    assert before["generator_sources"] == initial


@pytest.mark.asyncio
async def test_only_failed_unit_corrects_and_audits_new_fingerprint(harness):
    sessions, config, model = harness
    model.reject_ids = {"b"}
    result = await SummaryReviewService(sessions, config).analyze_documents("batch", sources("a", "b"), model)
    assert result.documents[0].model_dump() == candidate("a")
    assert "修订" in result.documents[1].summary_zh
    state = saved(sessions)
    audits = [event for event in state["history"] if event["event"] == "audit"]
    assert [event["unit_id"] for event in audits] == ["document:a", "document:b", "document:b"]
    assert audits[1]["fingerprint"] != audits[2]["fingerprint"]
    assert not audits[1]["audit"]["approved"] and audits[2]["audit"]["approved"]
    assert state["correction_rounds"] == {"document:b": 1}
    assert state["status"] == "ready"


@pytest.mark.asyncio
async def test_unchanged_correction_is_terminal_and_failure_code_survives(harness):
    sessions, config, model = harness
    model.reject_ids, model.unchanged = {"a"}, True
    for _ in range(2):
        service = SummaryReviewService(sessions, config)
        with pytest.raises(SummaryReviewPending):
            await service.analyze_documents("document:a", sources(), model)
    state = saved(sessions)
    assert state["status"] == "review_required" and state["failure_code"] == "correction_unchanged"
    assert model.count("SummaryCorrections") == 1 and model.count("SummaryAuditOutput") == 1
    assert not service.can_analyze_documents("document:a", sources())


@pytest.mark.asyncio
async def test_semantic_round_budget_persists_without_reauditing_rejected_candidate(harness):
    sessions, config, model = harness
    config.summary_review.max_correction_rounds = 1
    model.reject_ids, model.forever_reject = {"a"}, True
    for _ in range(3):
        with pytest.raises(SummaryReviewPending):
            await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert model.count("SummaryAuditOutput") == 2 and model.count("SummaryCorrections") == 1
    assert saved(sessions)["failure_code"] == "correction_budget_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["ReadingOutput", "SummaryAuditOutput", "SummaryCorrections"])
async def test_format_retry_reaches_actual_prompt_and_reserves_before_call(harness, stage):
    sessions, config, model = harness
    if stage == "SummaryCorrections":
        model.reject_ids = {"a"}
    model.responses[stage] = ["invalid-json SECRET response must not be saved"]
    observed = []

    async def inspect_reservation(_):
        state = saved(sessions)
        observed.append(state["history"][-1]["event"])
        assert state["owner"] and state["lease_until"] > now_iso()

    model.hook = inspect_reservation
    await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    prompts = [prompt for name, prompt in model.calls if name == stage]
    assert len(prompts) == 2 and prompts[1].startswith(FORMAT_FEEDBACK)
    assert all(event == "call" for event in observed)
    assert "SECRET" not in json.dumps(saved(sessions))


@pytest.mark.asyncio
async def test_format_budget_and_global_budget_are_durable(harness):
    sessions, config, model = harness
    model.responses["ReadingOutput"] = ["invalid", "invalid", "invalid"]
    for _ in range(2):
        with pytest.raises(SummaryReviewPending):
            await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert model.count("ReadingOutput") == 2
    assert saved(sessions)["failure_code"] == "format_budget_exhausted"
    assert not SummaryReviewService(sessions, config).can_analyze_documents("document:a", sources())


@pytest.mark.asyncio
async def test_global_call_budget_stops_before_creating_reviewer(harness, monkeypatch):
    sessions, config, model = harness
    config.summary_review.max_calls = 1
    monkeypatch.setattr("radar.summary_review.make_provider", lambda _: pytest.fail("Must not create exhausted reviewer"))
    with pytest.raises(SummaryReviewPending):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert len(model.calls) == 1 and saved(sessions)["failure_code"] == "global_call_budget_exhausted"
    assert not SummaryReviewService(sessions, config).can_analyze_documents("document:a", sources())


def http_error(status):
    request = httpx.Request("POST", "https://model.example.org")
    return httpx.HTTPStatusError("SECRET status response", request=request,
                                 response=httpx.Response(status, request=request))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [http_error(429), http_error(503), TimeoutError("SECRET"), httpx.ConnectError("SECRET")])
async def test_known_transport_failure_backs_off_and_resumes_same_stage(harness, failure):
    sessions, config, model = harness
    config.provider.kind = "openai_chat"
    model.responses["SummaryAuditOutput"] = [failure]
    service = SummaryReviewService(sessions, config)
    with pytest.raises(SummaryReviewPending):
        await service.analyze_documents("document:a", sources(), model)
    before = saved(sessions)
    assert before["status"] == "pending" and before["retry_at"] > now_iso()
    assert not service.can_analyze_documents("document:a", sources())
    with pytest.raises(SummaryReviewPending):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert len(model.calls) == 2
    due(sessions)
    assert service.can_analyze_documents("document:a", sources())
    await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    after = saved(sessions)
    assert model.count("ReadingOutput") == 1 and model.count("SummaryAuditOutput") == 2
    assert after["candidate"] == before["candidate"] and after["history"][:len(before["history"])] == before["history"]
    assert after["call_counts"]["request_upper_bound"] == 9
    assert "SECRET" not in json.dumps(after)


@pytest.mark.asyncio
async def test_transport_budget_and_backoff_survive_restarts(harness):
    sessions, config, model = harness
    model.responses["ReadingOutput"] = [http_error(503)] * 4
    for round_index in range(3):
        with pytest.raises(SummaryReviewPending):
            await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
        state = saved(sessions)
        if round_index < 2:
            remaining = (datetime.fromisoformat(state["retry_at"]) - datetime.now(UTC)).total_seconds()
            assert (55 if round_index == 0 else 295) < remaining <= (60 if round_index == 0 else 300)
            due(sessions)
    assert saved(sessions)["status"] == "error" and saved(sessions)["failure_code"] == "transport_budget_exhausted"
    with pytest.raises(SummaryReviewPending):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert model.count("ReadingOutput") == 3


@pytest.mark.asyncio
async def test_transport_and_format_have_separate_bounded_counters(harness):
    sessions, config, model = harness
    model.responses["ReadingOutput"] = [http_error(429), "invalid", http_error(503)]
    for _ in range(2):
        with pytest.raises(SummaryReviewPending):
            await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
        due(sessions)
    await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert model.count("ReadingOutput") == 4
    assert saved(sessions)["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [http_error(402), http_error(401), RuntimeError("SECRET token")])
async def test_nonretryable_failure_never_repeats_and_is_safe(harness, failure):
    sessions, config, model = harness
    model.responses["ReadingOutput"] = [failure]
    for _ in range(2):
        with pytest.raises(SummaryReviewPending) as caught:
            await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
        assert "SECRET" not in str(caught.value)
    assert len(model.calls) == 1 and "SECRET" not in json.dumps(saved(sessions))


@pytest.mark.asyncio
async def test_simultaneous_claim_allows_one_model_request(harness):
    sessions, config, model = harness
    entered, finish = asyncio.Event(), asyncio.Event()

    async def pause(name):
        if name == "ReadingOutput":
            entered.set()
            await finish.wait()

    model.hook = pause
    service = SummaryReviewService(sessions, config)
    first = asyncio.create_task(service.analyze_documents("document:a", sources(), model))
    await entered.wait()
    second = SummaryReviewService(sessions, config)
    assert not second.can_analyze_documents("document:a", sources())
    with pytest.raises(SummaryReviewPending):
        await second.analyze_documents("document:a", sources(), model)
    finish.set()
    await first
    assert model.count("ReadingOutput") == 1


@pytest.mark.asyncio
async def test_stale_owner_cannot_save_or_clear_new_owner_and_unknown_never_reposts(harness):
    sessions, config, model = harness

    async def supersede(_):
        with sessions.begin() as session:
            row = session.scalar(select(SummaryReview))
            row.owner = "different-owner"
            row.lease_until = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()

    model.hook = supersede
    service = SummaryReviewService(sessions, config)
    with pytest.raises(SummaryReviewPending):
        await service.analyze_documents("document:a", sources(), model)
    state = saved(sessions)
    assert state["candidate"] == {} and state["owner"] == "different-owner"
    with sessions.begin() as session:
        session.scalar(select(SummaryReview)).lease_until = ""
    assert not service.can_analyze_documents("document:a", sources())
    with pytest.raises(SummaryReviewPending):
        await service.analyze_documents("document:a", sources(), model)
    assert len(model.calls) == 1 and saved(sessions)["failure_code"] == "unknown_call_outcome"


@pytest.mark.asyncio
async def test_cancelled_call_persists_consumed_budget_and_never_retries(harness):
    sessions, config, model = harness
    model.responses["ReadingOutput"] = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert saved(sessions)["failure_code"] == "call_cancelled"
    with pytest.raises(SummaryReviewPending):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_prompt_limit_fails_closed_without_model_or_reserved_call(harness):
    sessions, config, model = harness
    config.summary_review.max_prompt_chars = 1000
    with pytest.raises(SummaryReviewPending):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert model.calls == [] and saved(sessions)["call_counts"] == {}
    assert saved(sessions)["failure_code"] == "prompt_too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["candidate", "evidence", "policy"])
async def test_ready_receipt_cannot_approve_changed_candidate_or_context(harness, change):
    sessions, config, model = harness
    service = SummaryReviewService(sessions, config)
    await service.analyze_documents("document:a", sources(), model)
    with sessions.begin() as session:
        row = session.scalar(select(SummaryReview))
        if change == "candidate":
            value = deepcopy(row.candidate)
            value["documents"][0]["summary_zh"] = "不同的候选事实需要独立审核。"
            row.candidate = value
        elif change == "evidence":
            value = deepcopy(row.evidence)
            value[0]["text"] = "Different original evidence"
            row.evidence = value
        else:
            row.policy = "different-policy"
    assert not service.can_analyze_documents("document:a", sources())
    with pytest.raises(SummaryReviewPending):
        await service.analyze_documents("document:a", sources(), model)
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_distinct_original_scope_and_configuration_have_independent_keys(harness):
    sessions, config, model = harness
    service = SummaryReviewService(sessions, config)
    await service.analyze_documents("document:a", sources(), model)
    changed = sources()
    changed[0]["text"] = "The source now describes six tools."
    await service.analyze_documents("document:a", changed, model)
    await service.analyze_documents("different-scope", sources(), model)
    config.summary_review.provider = ProviderConfig(kind="command", model="different-auditor")
    await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    with sessions() as session:
        assert len(session.scalars(select(SummaryReview)).all()) == 4


@pytest.mark.asyncio
async def test_documents_keep_original_chinese_and_id_parser_gates(harness):
    sessions, config, model = harness
    config.summary_review.max_format_retries = 0
    value = candidate("a")
    value["summary_zh"] = "English-only untranslated summary"
    model.responses["ReadingOutput"] = [json.dumps({"documents": [value]})]
    with pytest.raises(SummaryReviewPending):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert model.count("SummaryAuditOutput") == 0 and saved(sessions)["candidate"] == {}


@pytest.mark.asyncio
async def test_digest_header_and_each_story_require_approval(harness):
    sessions, config, model = harness
    result = await SummaryReviewService(sessions, config).generate_digest("digest:2026-09-08", sources("a", "b"), "2026-09-08", model)
    assert len(result.stories) == 2 and model.count("SummaryAuditOutput") == 3
    assert saved(sessions)["status"] == "ready"


def test_default_config_and_strict_budgets():
    config = SummaryReviewConfig()
    assert config.enabled and config.provider is None and config.max_calls == 64
    assert config.max_correction_rounds == 2 and config.max_format_retries == 1
    assert config.max_prompt_chars == 400000
    for field, value in [("max_calls", 0), ("max_calls", 501), ("max_calls", True), ("max_calls", "3"),
                         ("max_correction_rounds", -1), ("max_correction_rounds", 5),
                         ("max_format_retries", 3), ("max_prompt_chars", 999), ("max_prompt_chars", 2000001),
                         ("provider", {"kind": "extractive"})]:
        with pytest.raises(ValueError):
            SummaryReviewConfig.model_validate({field: value})
    assert SummaryReviewConfig(max_correction_rounds=0, max_format_retries=0, max_calls=500)


@pytest.mark.asyncio
async def test_correction_transport_resume_never_reaudits_same_semantic_rejection(harness):
    sessions, config, model = harness
    model.reject_ids = {"a"}
    model.responses["SummaryCorrections"] = [http_error(429)]
    with pytest.raises(SummaryReviewPending):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    original = saved(sessions)
    assert model.count("SummaryAuditOutput") == 1
    due(sessions)
    await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert model.count("SummaryAuditOutput") == 2 and model.count("SummaryCorrections") == 2
    final = saved(sessions)
    assert final["history"][:len(original["history"])] == original["history"]
    assert final["correction_rounds"] == {"document:a": 1}


@pytest.mark.asyncio
async def test_initial_generator_material_is_frozen_during_transport_recovery(harness):
    sessions, config, model = harness
    initial = sources()
    initial[0]["text_zh"] = "第一次冻结的已校对中文材料"
    model.responses["ReadingOutput"] = [http_error(429)]
    with pytest.raises(SummaryReviewPending):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", initial, model, evidence=sources())
    changed = sources()
    changed[0].update(text_zh="后来更新的派生内容", score=999)
    due(sessions)
    await SummaryReviewService(sessions, config).analyze_documents("document:a", changed, model, evidence=sources())
    prompts = [prompt for name, prompt in model.calls if name == "ReadingOutput"]
    assert prompts[0] == prompts[1]
    assert saved(sessions)["generator_sources"] == initial


@pytest.mark.asyncio
async def test_whole_call_timeout_ends_provider_and_keeps_durable_retry(harness, monkeypatch):
    sessions, config, model = harness
    ended = False
    original_timeout = asyncio.timeout
    deadlines = []

    def tiny_timeout(seconds):
        deadlines.append(seconds)
        return original_timeout(0.01)

    async def never_returns(_):
        nonlocal ended
        state = saved(sessions)
        assert (datetime.fromisoformat(state["lease_until"]) - datetime.now(UTC)).total_seconds() > config.provider.timeout_seconds + 55
        try:
            await asyncio.Event().wait()
        finally:
            ended = True

    monkeypatch.setattr("radar.summary_review.asyncio.timeout", tiny_timeout)
    model.hook = never_returns
    with pytest.raises(SummaryReviewPending):
        await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert ended and deadlines == [config.provider.timeout_seconds]
    assert saved(sessions)["failure_code"] == "provider_timeout"
    assert saved(sessions)["retry_at"] > now_iso()
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_correction_cycle_reuses_failed_receipt_and_does_not_reaudit(harness):
    sessions, config, model = harness
    model.reject_ids, model.forever_reject = {"a"}, True
    amended = candidate("a")
    amended["summary_zh"] += "不同的合成修订候选。"
    model.responses["SummaryCorrections"] = [json.dumps({"corrections": [{"unit_id": "document:a", "candidate": value}]})
                                               for value in (amended, candidate("a"))]
    for _ in range(2):
        with pytest.raises(SummaryReviewPending):
            await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    assert model.count("SummaryAuditOutput") == 2 and model.count("SummaryCorrections") == 2
    assert saved(sessions)["failure_code"] == "unchanged_call_not_repeated"


@pytest.mark.asyncio
async def test_invalid_cached_state_and_unserializable_inputs_return_safe_pending(harness):
    sessions, config, model = harness
    service = SummaryReviewService(sessions, config)
    invalid = sources()
    invalid[0]["derived"] = object()
    with pytest.raises(SummaryReviewPending):
        await service.analyze_documents("document:a", invalid, model, evidence=sources())
    assert model.calls == []
    await service.analyze_documents("document:a", sources(), model)
    with sessions.begin() as session:
        row = session.scalar(select(SummaryReview))
        row.candidate = {"SECRET invalid response": "PRIVATE candidate"}
    with pytest.raises(SummaryReviewPending) as error:
        await service.analyze_documents("document:a", sources(), model)
    assert "PRIVATE" not in str(error.value) and "SECRET" not in str(error.value)
    assert len(model.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("generation_kind,audit_kind", [("command", "openai_chat"), ("openai_chat", "codex")])
async def test_cli_request_upper_bound_is_unknown_even_with_api_other_stage(harness, generation_kind, audit_kind):
    sessions, config, model = harness
    config.provider.kind = generation_kind
    config.summary_review.provider = ProviderConfig(kind=audit_kind)
    await SummaryReviewService(sessions, config).analyze_documents("document:a", sources(), model)
    state = saved(sessions)
    assert state["call_counts"] == {"generation": 1, "audit": 1, "request_upper_bound": None}
    calls = [event for event in state["history"] if event["event"] == "call"]
    assert [event["request_upper_bound"] for event in calls] == ([None, 3] if generation_kind == "command" else [3, None])

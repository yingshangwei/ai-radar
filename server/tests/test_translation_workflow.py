"""Synthetic persistence regressions; no network or production records."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from openai import APIStatusError
from sqlalchemy import select

from radar import translation_workflow as workflow
from radar.config import TranslationConfig
from radar.db import database
from radar.models import Article, ArticleTranslation, Translation
from radar.translation import (
    RECHECK_POLICY,
    AuditedPart,
    TranslatedPart,
    TranslationLeaseError,
    TranslationService,
    candidate_fingerprint,
    ensure_translation,
)

SOURCE = "This AI model is not open source."
A = "这款人工智能模型已经开源。"
B = "这款人工智能模型完全开源。"
GOOD = "这款人工智能模型并未开源。"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    engine, sessions = database(f"sqlite:///{tmp_path}/workflow.db")
    config = TranslationConfig(enabled=True, concurrency=1)
    with sessions.begin() as session:
        key = ensure_translation(session, SOURCE, SOURCE, config).id
    yield sessions, config, key
    engine.dispose()


def read(sessions, key):
    with sessions() as session:
        row = session.get(Translation, key)
        return deepcopy({c.name: getattr(row, c.name) for c in row.__table__.columns})


def model_error(status):
    return APIStatusError("private synthetic response", body={}, response=httpx.Response(
        status, request=httpx.Request("POST", "https://example.invalid/model")))


def response(parts, text=GOOD):
    return {p["id"]: TranslatedPart(id=p["id"], zh=text, approved=True, issues=[]) for p in parts}


def verdict(parts, approved=True):
    return {p["id"]: AuditedPart(id=p["id"], approved=approved,
                                 issues=[] if approved else ["否定被改为肯定"])
            for p in parts}


async def test_already_chinese_pending_row_finalizes_without_model_calls(store):
    sessions, config, _ = store
    source = "这是一条已使用中文发布的信息。"
    with sessions.begin() as session:
        row = ensure_translation(session, source, source, config)
        key = row.id
        assert row.status == "pending"
    service = TranslationService(sessions, config)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Chinese source must not trigger paid translation")

    service.request = service.audit = service.auxiliary = forbidden
    with sessions() as session:
        assert service.can_finalize(session.get(Translation, key))
    await service.translate_one(key)
    result = read(sessions, key)
    assert result["status"] == "ready" and result["text_zh"] == source
    assert result["original_text"] == source and result["issues"] == []


async def test_pending_english_copy_cannot_bypass_audit(store):
    sessions, config, key = store
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.parts = [{"id": "body-0", "source": SOURCE, "draft": SOURCE,
                      "zh": SOURCE, "ok": True, "issues": []}]
    with sessions() as session:
        assert not TranslationService(sessions, config).can_finalize(session.get(Translation, key))


def seed_candidate(sessions, key, *, candidate=A, rounds=1, audit=False, flag=True):
    with sessions.begin() as session:
        row = session.get(Translation, key)
        part = deepcopy(row.parts[0])
        part.update(draft=candidate, zh=candidate, ok=False)
        if flag:
            part["correction_required"] = True
        part["review"] = {"policy": RECHECK_POLICY, "fingerprint": candidate_fingerprint(SOURCE, candidate),
                          "approved": True, "issues": [], "round": rounds}
        part["quality_history"] = [{"kind": "correction", **part["review"], "round": i + 1}
                                   for i in range(rounds)]
        if audit:
            part["audit"] = {"policy": RECHECK_POLICY, "fingerprint": candidate_fingerprint(SOURCE, candidate),
                             "approved": False, "issues": ["否定被改为肯定"]}
            part["quality_history"].append({"kind": "audit", **part["audit"]})
        row.parts, row.status = [part], "review_required"


@pytest.mark.asyncio
async def test_legacy_round_budget_is_not_reset_by_force_or_recheck(store):
    sessions, config, key = store
    seed_candidate(sessions, key, rounds=4, audit=True)
    before = read(sessions, key)
    for recheck in (False, True):
        service = TranslationService(sessions, config)

        async def forbidden(*args, **kwargs):
            raise AssertionError("No model is allowed after a persisted semantic budget")

        service.request = service.audit = forbidden
        await service.translate_one(key, force=True, recheck=recheck)
    assert read(sessions, key) == before


@pytest.mark.asyncio
async def test_legacy_denial_without_flag_can_use_remaining_real_repair(store):
    sessions, config, key = store
    seed_candidate(sessions, key, audit=True, flag=False)
    before = read(sessions, key)["parts"][0]["quality_history"]
    service, calls = TranslationService(sessions, config), []

    async def request(parts, *, review):
        calls.append("correction")
        assert review
        return response(parts)

    async def audit(parts):
        calls.append("audit")
        assert parts[0]["candidate"] == GOOD
        return verdict(parts)

    service.request, service.audit = request, audit
    await service.translate_one(key, force=True)
    after = read(sessions, key)
    assert after["status"] == "ready" and calls == ["correction", "audit"]
    assert after["parts"][0]["review"]["round"] == 2
    assert after["parts"][0]["quality_history"][:len(before)] == before


@pytest.mark.asyncio
async def test_a_b_a_candidate_cycle_keeps_receipts_and_stops_before_second_audit_a(store):
    sessions, config, key = store
    config = config.model_copy(update={"review_max_rounds": 4})
    calls, corrected = [], iter([A, B, A])
    service = TranslationService(sessions, config)

    async def request(parts, *, review):
        text = next(corrected) if review else A
        calls.append(("correction" if review else "draft", text))
        return response(parts, text)

    async def audit(parts):
        calls.append(("audit", parts[0]["candidate"]))
        return verdict(parts, False)

    service.request, service.audit = request, audit
    await service.translate_one(key)
    row = read(sessions, key)
    assert row["status"] == "review_required" and row["text_zh"] == ""
    assert [text for stage, text in calls if stage == "audit"] == [A, B]
    part = row["parts"][0]
    assert [r["round"] for r in part["quality_history"] if r["kind"] == "correction"] == [1, 2, 3]
    assert any(e.get("code") == "candidate_cycle" for e in part["workflow_history"])
    assert any(e.get("result", {}).get("zh") == B for e in part["workflow_history"])
    await TranslationService(sessions, config).translate_one(key, force=True, recheck=True)
    assert read(sessions, key) == row


@pytest.mark.asyncio
async def test_balance_resume_preserves_semantic_rounds_and_retries_only_unfinished_audit(store):
    sessions, config, key = store
    calls, reviews = [], 0
    first = TranslationService(sessions, config)

    async def request(parts, *, review):
        nonlocal reviews
        reviews += int(review)
        calls.append("correction" if review else "draft")
        return response(parts, B if reviews == 2 else A)

    async def audit(parts):
        calls.append("audit")
        if parts[0]["candidate"] == B:
            raise model_error(402)
        return verdict(parts, False)

    first.request, first.audit = request, audit
    await first.translate_one(key)
    before = read(sessions, key)
    assert before["status"] == "insufficient_balance" and before["attempts"] == 0
    assert workflow.correction_rounds(before["parts"][0], RECHECK_POLICY) == 2
    resumed = TranslationService(sessions, config)

    async def last_audit(parts):
        calls.append("audit")
        return verdict(parts, False)

    resumed.audit = last_audit
    await resumed.translate_one(key, force=True)
    after = read(sessions, key)
    assert after["status"] == "review_required"
    assert calls == ["draft", "correction", "audit", "correction", "audit", "audit"]
    assert workflow.correction_rounds(after["parts"][0], RECHECK_POLICY) == 2
    await TranslationService(sessions, config).translate_one(key, force=True)
    assert read(sessions, key) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [429, 503, "timeout", "connection"])
async def test_known_transport_has_persistent_finite_recovery_budget(store, error):
    sessions, config, key = store
    calls = []

    async def request(parts, *, review):
        calls.append("request")
        if error == "timeout":
            raise TimeoutError("private timeout")
        if error == "connection":
            raise httpx.ConnectError("private connection")
        raise model_error(error)

    for _ in range(5):
        service = TranslationService(sessions, config)
        service.request = request
        await service.translate_one(key, force=True)
    row = read(sessions, key)
    assert calls == ["request"] * 3 and row["status"] == "error"
    assert row["attempts"] == 3
    assert workflow.correction_rounds(row["parts"][0], RECHECK_POLICY) == 0
    assert "private" not in json.dumps(row["issues"])


@pytest.mark.asyncio
async def test_known_transport_natural_retry_respects_backoff(store):
    sessions, config, key = store
    calls = []
    service = TranslationService(sessions, config)

    async def request(parts, *, review):
        calls.append("request")
        raise TimeoutError("synthetic")

    service.request = request
    await service.translate_one(key)
    await service.translate_one(key)
    assert len(calls) == 1
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.retry_at = ""  # Even an expired outer retry must not override the ledger's backoff.
    await service.translate_one(key)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["json", "literal"])
async def test_each_actual_format_recovery_request_has_a_pre_call_reservation(store, failure):
    sessions, config, key = store
    if failure == "literal":
        with sessions.begin() as session:
            key = ensure_translation(session, "AI source", "AI source https://example.org/a", config).id
    service, stages = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        stages.append(stage)
        parts = payload["untrusted_parts"]
        row = read(sessions, key)
        for part in row["parts"]:
            if part["id"] in {p["id"] for p in parts}:
                records = part["workflow_history"]
                assert records[-1]["kind"] == "request_reserved"
                assert records[-1]["sdk_request_upper_bound"] == 2
                assert any(e["kind"] == "reserved" and e["call_id"] == records[-1]["call_id"] for e in records)
        if len(stages) == 1:
            if failure == "json":
                return "{"
            return json.dumps({"translations": [dict(id=p["id"], zh="人工智能。", approved=True, issues=[])
                                                 for p in parts]})
        if stage == "audit":
            return json.dumps({"audits": [dict(id=p["id"], approved=True, issues=[]) for p in parts]})
        return json.dumps({"translations": [dict(
            id=p["id"], zh="人工智能来源 " + ("⟪原文链接-0⟫" if "⟪原文链接-0⟫" in p["source"] else ""),
            approved=True, issues=[]) for p in parts]})

    service._completion = completion
    await service.translate_one(key)
    assert stages == ["draft", "draft", "correction", "audit"]
    row = read(sessions, key)
    assert row["status"] == "ready"
    for part in row["parts"]:
        assert [e["stage"] for e in part["workflow_history"] if e["kind"] == "request_reserved"] == stages
        assert len([e for e in part["workflow_history"] if e["kind"] == "reserved"]) == 3


@pytest.mark.asyncio
async def test_response_application_error_leaves_unknown_reservation_and_never_retries(store):
    sessions, config, key = store
    service, calls = TranslationService(sessions, config), []

    async def incomplete(parts, *, review):
        calls.append("request")
        return {}  # Adapter returned but applying this result fails before a durable completion.

    service.request = incomplete
    await service.translate_one(key)
    row = read(sessions, key)
    assert row["status"] == "error" and workflow.blocked(row["parts"][0], RECHECK_POLICY)
    second = TranslationService(sessions, config)
    second.request = incomplete
    await second.translate_one(key, force=True)
    assert calls == ["request"] and read(sessions, key) == row


@pytest.mark.asyncio
async def test_expired_owner_cannot_save_or_publish_returned_result(store):
    sessions, config, key = store
    service, replacement = TranslationService(sessions, config), {}

    async def request(parts, *, review):
        with sessions.begin() as session:
            row = session.get(Translation, key)
            row.owner = "replacement-owner"
            row.lease_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        replacement.update(read(sessions, key))
        return response(parts)

    service.request = request
    await service.translate_one(key)
    assert read(sessions, key) == replacement
    with pytest.raises(TranslationLeaseError):
        service.save_parts(key, "old-owner", [])
    assert read(sessions, key) == replacement


def test_recheck_history_is_a_deep_frozen_snapshot(store):
    sessions, config, key = store
    seed_candidate(sessions, key, audit=True)
    with sessions() as session:
        row = session.get(Translation, key)
        parts = deepcopy(row.parts)
        parts[0]["workflow_history"] = [workflow.event(parts[0], RECHECK_POLICY, "baseline", correction_rounds=1)]
        original = deepcopy(parts[0])
        TranslationService(sessions, config).prepare_recheck(row, parts, {})
    parts[0]["quality_history"].append({"kind": "synthetic-new-receipt"})
    parts[0]["workflow_history"].append({"kind": "synthetic-new-call"})
    assert parts[0]["review_history"][-1]["previous"] == original


@pytest.mark.asyncio
async def test_unclassified_legacy_error_cannot_be_unlocked_with_force(store):
    sessions, config, key = store
    with sessions.begin() as session:
        session.get(Translation, key).status = "error"
    before = read(sessions, key)
    await TranslationService(sessions, config).translate_one(key, force=True)
    await TranslationService(sessions, config).translate_one(key, force=True, recheck=True)
    assert read(sessions, key) == before


@pytest.mark.asyncio
async def test_successful_historical_receipt_reuse_does_not_rewrite_old_history(store):
    sessions, config, key = store
    seed_candidate(sessions, key, candidate=GOOD)
    with sessions.begin() as session:
        row = session.get(Translation, key)
        part = deepcopy(row.parts[0])
        part["correction_required"] = False
        part["quality_history"].append({"kind": "audit", "policy": RECHECK_POLICY,
            "fingerprint": candidate_fingerprint(SOURCE, GOOD), "approved": True, "issues": [],
            "machine_issues": [], "correction_issues": []})
        row.parts = [part]
    before = read(sessions, key)
    await TranslationService(sessions, config).translate_one(key)
    after = read(sessions, key)
    assert after["status"] == "ready" and after["text_zh"] == GOOD
    assert after["parts"][0]["quality_history"] == before["parts"][0]["quality_history"]
    assert not any(e["kind"] == "reserved" for e in after["parts"][0]["workflow_history"])


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["machine_issues", "correction_issues"])
async def test_historical_machine_or_correction_failure_cannot_be_laundered_by_receipt_reuse(store, field):
    sessions, config, key = store
    seed_candidate(sessions, key, candidate=GOOD, rounds=2)
    with sessions.begin() as session:
        row = session.get(Translation, key)
        part = deepcopy(row.parts[0])
        part["correction_required"] = False
        part["quality_history"].append({"kind": "audit", "policy": RECHECK_POLICY,
            "fingerprint": candidate_fingerprint(SOURCE, GOOD), "approved": True, "issues": [], field: ["既有未通过问题"]})
        row.parts = [part]
    await TranslationService(sessions, config).translate_one(key)
    after = read(sessions, key)
    assert after["status"] == "review_required" and after["text_zh"] == ""
    assert "既有未通过问题" in after["parts"][0]["issues"]


@pytest.mark.asyncio
async def test_terminal_head_is_filtered_before_limit_without_mutating_it(store, monkeypatch):
    sessions, config, key = store
    config = config.model_copy(update={"max_documents": 1})
    seed_candidate(sessions, key, rounds=4, audit=True)
    with sessions.begin() as session:
        for name, text, when in [("blocked", SOURCE, "2026-09-08T12:00:00+00:00"),
                                 ("next", "AI models support research.", "2026-09-08T11:00:00+00:00")]:
            a = Article(id=name, platform="x", external_id=name, source_id="fixture", title=text, text=text,
                        url=f"https://example.invalid/{name}", canonical_url=f"https://example.invalid/{name}",
                        author="Synthetic", published_at=when)
            session.add(a)
            cached = ensure_translation(session, text, text, config)
            session.flush()
            session.add(ArticleTranslation(article_id=name, translation_id=cached.id))
    before = read(sessions, key)
    called = []

    async def request(service, parts, *, review):
        called.extend(p["source"] for p in parts)
        return response(parts, "人工智能模型支持研究。")

    async def audit(service, parts):
        return verdict(parts)

    monkeypatch.setattr(TranslationService, "request", request)
    monkeypatch.setattr(TranslationService, "audit", audit)
    await TranslationService(sessions, config).pending(force=True)
    assert called == ["AI models support research."] * 2
    assert read(sessions, key) == before
    with sessions() as session:
        assert set(session.scalars(select(Translation.status))) == {"ready", "review_required"}


@pytest.mark.asyncio
async def test_restored_initial_candidate_gets_its_first_independent_audit(store):
    sessions, config, key = store
    service, calls = TranslationService(sessions, config), []
    candidates = iter([GOOD, A, GOOD])

    async def request(parts, *, review):
        candidate = next(candidates)
        calls.append(("correction" if review else "draft", candidate))
        return response(parts, candidate)

    async def audit(parts):
        calls.append(("audit", parts[0]["candidate"]))
        return verdict(parts, parts[0]["candidate"] == GOOD)

    service.request, service.audit = request, audit
    await service.translate_one(key)
    assert calls == [("draft", GOOD), ("correction", A), ("audit", A), ("correction", GOOD), ("audit", GOOD)]
    assert read(sessions, key)["status"] == "ready"


@pytest.mark.asyncio
async def test_committed_receipts_finalize_after_crash_without_new_calls_or_budget(store):
    sessions, config, key = store
    service = TranslationService(sessions, config)

    async def request(parts, *, review):
        return response(parts)

    async def audit(parts):
        return verdict(parts)

    service.request, service.audit = request, audit
    finish_review = service.review_parts

    async def crash_after_receipts(*args, **kwargs):
        await finish_review(*args, **kwargs)
        raise SystemExit(77)  # All receipts are durable; publication transaction has not run.

    service.review_parts = crash_after_receipts
    with pytest.raises(SystemExit):
        await service.translate_one(key)
    before = read(sessions, key)
    assert before["status"] == "error" and before["text_zh"] == ""
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.status, row.attempts = "running", config.max_attempts
        row.lease_until = ""
    await TranslationService(sessions, config).translate_one(key)
    after = read(sessions, key)
    assert after["status"] == "ready" and after["text_zh"] == GOOD
    assert after["attempts"] == config.max_attempts and after["parts"] == before["parts"]

import asyncio
import json

import httpx
import pytest
from openai import APIStatusError
from pydantic import ValidationError

from radar.config import TranslationConfig
from radar.db import database
from radar.models import Translation
from radar.translation import (
    RECHECK_POLICY,
    AuditedPart,
    TranslatedPart,
    TranslationService,
    ensure_translation,
    needs_recheck,
)

SOURCE = "The AI product could earn $5m revenue on the team's own cloud computer."
GOOD = "这款 AI 产品可能在团队自己的云端计算机上获得 $5m 收入。"
BAD = "这款 AI 产品已经在私有云上实现 $5m 利润。"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/audit.db")
    config = TranslationConfig(enabled=True, audit_model="independent-auditor")
    with sessions.begin() as session:
        key = ensure_translation(session, SOURCE, SOURCE, config).id
    yield sessions, config, key
    engine.dispose()


def translate(parts, text=GOOD, approved=True):
    return {p["id"]: TranslatedPart(id=p["id"], zh=text, approved=approved) for p in parts}


def approve(parts):
    return {p["id"]: AuditedPart(id=p["id"], approved=True) for p in parts}


@pytest.mark.asyncio
async def test_semantic_audit_rejects_editor_approval_and_drives_bounded_repair(setup):
    sessions, config, key = setup
    service = TranslationService(sessions, config)
    calls = []

    async def request(parts, *, review):
        calls.append("review" if review else "draft")
        if calls.count("review") == 2:
            assert "可能性被强化为已经实现，收入误译成利润，自己的电脑误译为私有云" in parts[0]["checks"]
            return translate(parts)
        return translate(parts, BAD)

    async def audit(parts):
        calls.append("audit")
        assert set(parts[0]) == {"id", "source", "candidate"}
        if parts[0]["candidate"] == BAD:
            return {parts[0]["id"]: AuditedPart(
                id=parts[0]["id"], approved=False,
                issues=["可能性被强化为已经实现，收入误译成利润，自己的电脑误译为私有云"],
            )}
        return approve(parts)

    service.request, service.audit = request, audit
    await service.translate_one(key)
    assert calls == ["draft", "review", "audit", "review", "audit"]
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "ready" and row.text_zh == GOOD and row.original_text == SOURCE
        assert row.parts[0]["initial_draft"] == BAD
        assert row.parts[0]["audit"]["model"] == "independent-auditor"
        assert row.parts[0]["audit"]["policy"] == RECHECK_POLICY
        assert row.parts[0]["review"]["round"] == 2
        assert [item["kind"] for item in row.parts[0]["quality_history"]] == [
            "correction", "audit", "correction", "audit"
        ]
        assert not needs_recheck(row)
    await service.translate_one(key, force=True)
    await service.translate_one(key, recheck=True)
    assert len(calls) == 5  # Neither force-pending nor idempotent recheck redrafts ready content.


@pytest.mark.asyncio
async def test_persistent_semantic_rejection_stops_at_configured_rounds(setup):
    sessions, config, key = setup
    service = TranslationService(sessions, config.model_copy(update={"review_max_rounds": 1}))
    calls = []

    async def request(parts, *, review):
        calls.append("review" if review else "draft")
        return translate(parts, BAD)

    async def audit(parts):
        calls.append("audit")
        return {p["id"]: AuditedPart(id=p["id"], approved=False, issues=["语义不忠实"]) for p in parts}

    service.request, service.audit = request, audit
    await service.translate_one(key)
    assert calls == ["draft", "review", "audit"]
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "review_required" and row.text_zh == ""
        assert row.parts[0]["draft"] == BAD and row.issues == ["语义不忠实"]
    await service.translate_one(key)
    assert len(calls) == 3  # Normal backoff still applies.


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["number", "url", "correction_rejected"])
async def test_audit_approval_never_overrides_deterministic_or_editor_failures(setup, fault):
    sessions, config, key = setup
    if fault == "url":
        with sessions.begin() as session:
            row = session.get(Translation, key)
            row.parts = [{"id": "body-0", "source": SOURCE + " https://example.org/paper"}]
    service = TranslationService(sessions, config)

    async def request(parts, *, review):
        text = GOOD.replace("$5m", "$6m") if fault == "number" else GOOD
        return translate(parts, text, approved=fault != "correction_rejected")

    async def audit(parts):
        return approve(parts)

    service.request, service.audit = request, audit
    await service.translate_one(key)
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "review_required" and not row.text_zh
        assert row.parts[0]["audit"]["approved"] and row.issues


@pytest.mark.asyncio
async def test_audit_402_preserves_review_and_resumes_without_redraft_or_correction(setup):
    sessions, config, key = setup
    service = TranslationService(sessions, config)
    calls = []

    async def request(parts, *, review):
        calls.append("review" if review else "draft")
        return translate(parts)

    async def unavailable(parts):
        calls.append("audit-402")
        raise APIStatusError("private secret", response=httpx.Response(
            402, request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        ), body={})

    service.request, service.audit = request, unavailable
    await service.translate_one(key)
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "insufficient_balance" and row.attempts == 0
        assert row.parts[0]["draft"] == GOOD and row.parts[0]["review"]["approved"]
        assert not row.parts[0]["correction_required"]
        assert "private" not in str(row.issues)
    restarted = TranslationService(sessions, config)
    restarted.request = request

    async def recovered(parts):
        calls.append("audit")
        return approve(parts)

    restarted.audit = recovered
    await restarted.translate_one(key, force=True)
    assert calls == ["draft", "review", "audit-402", "audit"]
    with sessions() as session:
        assert session.get(Translation, key).status == "ready"


@pytest.mark.asyncio
async def test_ready_recheck_cas_lease_deduplicates_independent_workers(setup):
    sessions, config, key = setup
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.status, row.text_zh = "ready", GOOD
        row.parts = [{"id": "body-0", "source": SOURCE, "draft": GOOD, "zh": GOOD, "ok": True}]
    first, second = TranslationService(sessions, config), TranslationService(sessions, config)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def first_audit(parts):
        calls.append("first")
        entered.set()
        await release.wait()
        return approve(parts)

    async def second_audit(parts):
        calls.append("second")
        return approve(parts)

    first.audit, second.audit = first_audit, second_audit
    pending = asyncio.create_task(first.translate_one(key, recheck=True))
    await entered.wait()
    await second.translate_one(key, recheck=True)
    release.set()
    await pending
    assert calls == ["first"]
    await second.translate_one(key, force=True, recheck=True)
    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_recheck_restores_only_affected_machine_parts_and_archives_provenance(setup):
    sessions, config, key = setup
    machine_parts = [GOOD, GOOD + " 工具。", GOOD + " 模型。"]
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.status, row.text_zh, row.review_model = "ready", "manual", "old + Codex editorial review"
        row.parts = [dict(id=f"body-{i}", source=SOURCE, zh=machine, draft=machine, ok=True)
                     for i, machine in enumerate(machine_parts)]
        row.parts[0].update(zh="manual one", draft="manual one", editorial_previous_zh=machine_parts[0])
        row.parts[1].update(zh="manual two", draft="manual two", editorial_previous=machine_parts[1])
    service = TranslationService(sessions, config)
    candidates = []

    async def no_draft(*args, **kwargs):
        raise AssertionError("Preserved machine candidates must not be redrafted")

    async def audit(parts):
        candidates.extend(p["candidate"] for p in parts)
        return approve(parts)

    service.request, service.audit = no_draft, audit
    await service.translate_one(key, recheck=True)
    assert candidates == machine_parts
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "ready" and row.text_zh == "\n\n".join(machine_parts)
        assert not needs_recheck(row) and "editorial" not in row.review_model
        for part in row.parts:
            assert not any(k.startswith("editorial_") for k in part)
            assert "recheck_pending" not in part
            assert part["review_history"][0]["row_provenance"]["status"] == "ready"
        assert row.parts[0]["review_history"][0]["previous"]["zh"] == "manual one"


@pytest.mark.asyncio
async def test_legacy_without_machine_candidate_redrafts_from_source_without_manual_inputs(setup):
    sessions, config, key = setup
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.status, row.text_zh, row.review_model = "ready", "manual", "old + Codex editorial review"
        row.parts = [{"id": "body-0", "source": SOURCE, "draft": "manual", "zh": "manual", "ok": True}]
    service = TranslationService(sessions, config)
    calls = []

    async def request(parts, *, review):
        calls.append("review" if review else "draft")
        assert "manual" not in json.dumps(parts)
        return translate(parts)

    async def audit(parts):
        calls.append("audit")
        return approve(parts)

    service.request, service.audit = request, audit
    await service.translate_one(key, recheck=True)
    assert calls == ["draft", "review", "audit"]


@pytest.mark.asyncio
async def test_failed_legacy_recheck_resumes_retained_machine_candidate(setup):
    sessions, config, key = setup
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.status, row.text_zh, row.review_model = "ready", "manual", "old + Codex editorial review"
        row.parts = [{"id": "body-0", "source": SOURCE, "zh": "manual", "draft": "manual",
                      "ok": True, "editorial_previous_zh": GOOD}]
    service = TranslationService(sessions, config)
    calls = []

    async def no_draft(*args, **kwargs):
        raise AssertionError("Restored machine candidate must survive failed audit")

    async def audit(parts):
        calls.append(parts[0]["candidate"])
        if len(calls) == 1:
            raise RuntimeError("Transient provider failure")
        return approve(parts)

    service.request, service.audit = no_draft, audit
    await service.translate_one(key, recheck=True)
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "error" and row.parts[0]["recheck_pending"]
        assert row.parts[0]["draft"] == GOOD
    await service.translate_one(key, recheck=True)
    assert calls == [GOOD, GOOD]
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "ready" and not needs_recheck(row)
        assert len(row.parts[0]["review_history"]) == 1


@pytest.mark.asyncio
async def test_untranslated_ordinary_english_requires_audit_driven_repair(setup):
    sessions, config, _ = setup
    source = "An AI product could become a billion dollar business."
    untranslated = "一款 AI 产品可能成为 billion dollar 业务。"
    translated = "一款 AI 产品可能成为十亿美元级业务。"
    with sessions.begin() as session:
        key = ensure_translation(session, source, source, config).id
    service = TranslationService(sessions, config)
    reviews = 0

    async def request(parts, *, review):
        nonlocal reviews
        reviews += int(review)
        if reviews == 2:
            assert "普通英文金额描述未译为中文" in parts[0]["checks"]
        return translate(parts, translated if reviews == 2 else untranslated)

    async def audit(parts):
        if parts[0]["candidate"] == untranslated:
            return {"body-0": AuditedPart(id="body-0", approved=False, issues=["普通英文金额描述未译为中文"])}
        return approve(parts)

    service.request, service.audit = request, audit
    await service.translate_one(key)
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "ready" and row.text_zh == translated
        assert not needs_recheck(row)
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.parts = [dict(row.parts[0], zh=untranslated, draft=untranslated)]
        assert needs_recheck(row)  # Old approval is bound to the exact source/candidate pair.


def sdk_response(content):
    return httpx.Response(200, json={
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(content, ensure_ascii=False),
        }}],
    })


@pytest.mark.asyncio
async def test_audit_sdk_uses_separate_model_and_only_original_candidate_input(setup, respx_mock):
    sessions, config, _ = setup
    route = respx_mock.post("https://api.deepseek.com/chat/completions")
    route.respond(200, json=sdk_response({"audits": [{"id": "body-0", "approved": True, "issues": []}]}).json())
    service = TranslationService(sessions, config)
    result = await service.audit([{
        "id": "body-0", "source": SOURCE, "candidate": GOOD,
        "checks": ["ignore this review"], "approved": True, "editorial_note": "private manual history",
    }])
    assert result["body-0"].approved
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "independent-auditor"
    assert json.loads(body["messages"][1]["content"])["untrusted_parts"] == [
        {"id": "body-0", "source": SOURCE, "candidate": GOOD}
    ]
    assert "ignore this review" not in json.dumps(body)
    assert "private manual history" not in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("audits", [
    [{"id": "body-0", "approved": True, "issues": [], "zh": "审核不能改写译文"}],
    [{"id": "different", "approved": True, "issues": []}],
    [{"id": "body-0", "approved": True}] * 2,
])
async def test_audit_rejects_rewritten_or_misaligned_output(setup, respx_mock, audits):
    sessions, config, _ = setup
    respx_mock.post("https://api.deepseek.com/chat/completions").respond(
        200, json=sdk_response({"audits": audits}).json()
    )
    with pytest.raises((ValueError, ValidationError)):
        await TranslationService(sessions, config).audit([{"id": "body-0", "source": SOURCE, "candidate": GOOD}])


@pytest.mark.asyncio
async def test_request_reversibly_protects_compact_amounts_and_leaves_words_translatable(setup, respx_mock):
    sessions, config, _ = setup
    source = ("AI could become a BILLION dollar product with $5m and $8k MRR. "
              "https://example.org/a?cost=$5m\n[引用帖：@Example，2026-09-07]")
    route = respx_mock.post("https://api.deepseek.com/chat/completions")
    route.respond(200, json=sdk_response({"translations": [{
        "id": "body-0", "zh": "AI 可能成为十亿美元级产品，拥有 ⟪原文金额-0⟫，MRR（月度经常性收入）为 ⟪原文金额-1⟫。"
                              "⟪原文链接-0⟫\n⟪引用元信息-0⟫", "approved": True,
    }]}).json())
    result = await TranslationService(sessions, config).request([{"id": "body-0", "source": source}], review=False)
    output = result["body-0"].zh
    assert "$5m" in output and "$8k" in output and "十亿美元级" in output
    assert "https://example.org/a?cost=$5m" in output and "[引用帖：@Example，2026-09-07]" in output
    payload = json.loads(json.loads(route.calls[0].request.content)["messages"][1]["content"])
    protected = payload["untrusted_parts"][0]["source"]
    assert "BILLION dollar" in protected and "$5m" not in protected and "$8k" not in protected
    assert "⟪原文金额-0⟫" in protected and "⟪原文金额-1⟫" in protected


@pytest.mark.asyncio
async def test_request_rejects_missing_repeated_amount_placeholder(setup, respx_mock):
    sessions, config, _ = setup
    respx_mock.post("https://api.deepseek.com/chat/completions").respond(200, json=sdk_response({
        "translations": [{"id": "body-0", "zh": "AI 的收入为 ⟪原文金额-0⟫。", "approved": True}],
    }).json())
    with pytest.raises(ValueError, match="金额"):
        await TranslationService(sessions, config).request([
            {"id": "body-0", "source": "The AI raised $5m and plans another $5m."}
        ], review=False)


def test_audit_configuration_defaults_and_bounds():
    assert TranslationConfig().audit_model is None
    assert TranslationConfig().review_max_rounds == 2
    for invalid in [0, 5]:
        with pytest.raises(ValidationError):
            TranslationConfig(review_max_rounds=invalid)

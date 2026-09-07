import json
from copy import deepcopy

import httpx
import pytest
from openai import APIStatusError

from radar.config import TranslationConfig
from radar.db import database
from radar.models import Translation, TranslationAccountState
from radar.translation import (
    AUDIT,
    POLICY,
    REVIEW,
    TranslationOutput,
    TranslationService,
    TranslationValidationError,
    account_scope,
    ensure_translation,
    part_audited,
)

SOURCE = "AI agents could earn $5m. https://example.org/paper"
CHINESE = "AI 智能体可能获得 $5m。https://example.org/paper"
PROTECTED = "AI 智能体可能获得 ⟪原文金额-0⟫。⟪原文链接-0⟫"
PRIVATE = "sk-private-response-field-and-input"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/request-format.db")
    config = TranslationConfig(enabled=True, audit_model="separate-auditor")
    with sessions.begin() as session:
        key = ensure_translation(session, SOURCE, SOURCE, config).id
    yield sessions, config, key
    engine.dispose()


def translated(*, uid="body-0", zh=PROTECTED, approved=True, issues=()):
    return json.dumps({"translations": [
        {"id": uid, "zh": zh, "approved": approved, "issues": list(issues)}
    ]})


def malformed(fault):
    if fault == "json":
        return '{"translations":[' + PRIVATE + CHINESE
    output = json.loads(translated())
    if fault == "missing_approval":
        del output["translations"][0]["approved"]
    elif fault == "extra_field":
        output["translations"][0][PRIVATE] = SOURCE + CHINESE
    else:
        raise AssertionError("Unknown fixture")
    return json.dumps(output)


def assert_request_payload(payload, system, model, config, *, review):
    assert system == POLICY + (REVIEW if review else "")
    assert model == (config.review_model if review else config.model)
    parts = payload["untrusted_parts"]
    assert len(parts) == 1 and parts[0]["id"] == "body-0"
    assert parts[0]["source"] == "AI agents could earn ⟪原文金额-0⟫. ⟪原文链接-0⟫"
    assert PRIVATE not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("fault", ["missing_approval", "json", "extra_field"])
async def test_request_retries_invalid_format_once_with_safe_feedback_and_unchanged_literals(setup, review, fault):
    sessions, config, _ = setup
    service, calls = TranslationService(sessions, config), []
    inputs = [{"id": "body-0", "source": SOURCE}]
    if review:
        inputs[0].update(draft=CHINESE, checks=["逐项核对原文"])
    before = deepcopy(inputs)

    async def completion(payload, *, system, model, stage):
        assert_request_payload(payload, system, model, config, review=review)
        calls.append(deepcopy(payload))
        if len(calls) == 1:
            assert set(payload) == {"glossary", "untrusted_parts"}
            return malformed(fault)
        assert len(calls) == 2 and set(payload) == {"glossary", "untrusted_parts", "format_feedback"}
        assert payload["untrusted_parts"] == calls[0]["untrusted_parts"]
        assert payload["glossary"] == calls[0]["glossary"]
        feedback = payload["format_feedback"]
        assert set(feedback) == {"reason", "errors", "required_schema"}
        assert feedback["reason"] == "output_schema_invalid"
        assert feedback["required_schema"] == TranslationOutput.model_json_schema()
        assert all(set(item) == {"type", "loc"} for item in feedback["errors"])
        expected = {
            "missing_approval": [{"type": "missing", "loc": ["translations", 0, "approved"]}],
            "json": [{"type": "json_invalid", "loc": []}],
            "extra_field": [{"type": "extra_forbidden", "loc": ["translations", 0, "unknown_field"]}],
        }
        assert feedback["errors"] == expected[fault]
        assert all(private not in json.dumps(feedback, ensure_ascii=False)
                   for private in [SOURCE, CHINESE, PRIVATE, PROTECTED])
        return translated()

    service._completion = completion
    result = await service.request(inputs, review=review)
    assert len(calls) == 2 and inputs == before
    assert result["body-0"].zh == CHINESE and result["body-0"].approved


@pytest.mark.asyncio
async def test_recovered_draft_still_requires_correction_and_independent_audit_before_publication(setup):
    sessions, config, key = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        if system == AUDIT:
            calls.append("audit")
            assert model == "separate-auditor"
            assert payload["untrusted_parts"] == [{"id": "body-0", "source": SOURCE, "candidate": CHINESE}]
            assert "format_feedback" not in payload
            with sessions() as session:
                row = session.get(Translation, key)
                assert row.status == "running" and not row.text_zh
                assert row.parts[0]["draft"] == CHINESE and row.parts[0]["review"]["approved"]
            return json.dumps({"audits": [{"id": "body-0", "approved": True, "issues": []}]})
        review = system == POLICY + REVIEW
        assert_request_payload(payload, system, model, config, review=review)
        calls.append("review" if review else "draft")
        if calls == ["draft"]:
            return malformed("missing_approval")
        assert ("format_feedback" in payload) == (calls == ["draft", "draft"])
        return translated()

    service._completion = completion
    await service.translate_one(key)
    assert calls == ["draft", "draft", "review", "audit"]
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "ready" and row.text_zh == CHINESE and row.original_text == SOURCE
        assert part_audited(row.parts[0]) and row.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("fault", ["missing_approval", "json"])
async def test_second_invalid_request_stays_failed_without_guessing_approval(setup, review, fault, caplog):
    sessions, config, key = setup
    service, invalid_calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        is_review = system == POLICY + REVIEW
        assert_request_payload(payload, system, model, config, review=is_review)
        if review and not is_review:
            return translated()
        invalid_calls.append(deepcopy(payload))
        return malformed(fault)

    service._completion = completion
    await service.translate_one(key)
    assert len(invalid_calls) == 2
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "error" and not row.text_zh and not row.title_zh
        assert "output_schema_invalid" in row.issues[0]
        assert not row.owner and not row.lease_until and row.attempts == 1
        assert "review" not in row.parts[0] and "audit" not in row.parts[0]
        if review:
            assert row.parts[0]["draft"] == CHINESE
        else:
            assert "draft" not in row.parts[0]
        assert PRIVATE not in json.dumps(row.issues)
    assert PRIVATE not in caplog.text and CHINESE not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["rejected", "issues"])
async def test_valid_review_rejection_is_not_format_retried_and_cannot_publish(setup, fault):
    sessions, config, key = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        assert "format_feedback" not in payload
        if system == AUDIT:
            calls.append("audit")
            return json.dumps({"audits": [{"id": "body-0", "approved": True, "issues": []}]})
        review = system == POLICY + REVIEW
        assert_request_payload(payload, system, model, config, review=review)
        calls.append("review" if review else "draft")
        return translated(approved=not review or fault != "rejected",
                          issues=["限定条件仍有疑点"] if review and fault == "issues" else [])

    service._completion = completion
    await service.translate_one(key)
    assert calls == ["draft", "review", "audit", "review", "audit"]
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "review_required" and not row.text_zh and row.issues
        assert row.parts[0]["review"]["round"] == config.review_max_rounds
        assert row.parts[0]["audit"]["approved"] and row.parts[0]["audit"]["correction_issues"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unknown_id", "duplicate_id", "missing_literal", "duplicate_literal"])
async def test_ids_fail_immediately_and_literal_failures_exhaust_one_shared_retry(setup, fault):
    sessions, config, _ = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        assert_request_payload(payload, system, model, config, review=False)
        calls.append(deepcopy(payload))
        if len(calls) == 1:
            assert "format_feedback" not in payload
        else:
            assert len(calls) == 2 and "literal" in fault
            assert payload["format_feedback"]["reason"] == "protected_literal_mismatch"
        output = json.loads(translated())
        if fault == "unknown_id":
            output["translations"][0]["id"] = "unknown"
        elif fault == "duplicate_id":
            output["translations"].append(deepcopy(output["translations"][0]))
        elif fault == "missing_literal":
            output["translations"][0]["zh"] = PROTECTED.replace("⟪原文链接-0⟫", "")
        else:
            output["translations"][0]["zh"] += "⟪原文金额-0⟫"
        return json.dumps(output)

    service._completion = completion
    with pytest.raises(TranslationValidationError) as caught:
        await service.request([{"id": "body-0", "source": SOURCE}], review=False)
    assert caught.value.code == ("translation_part_mismatch" if "id" in fault else "protected_literal_mismatch")
    assert len(calls) == (1 if "id" in fault else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402])
@pytest.mark.parametrize("after_format_error", [False, True])
async def test_http_errors_preserve_auth_or_balance_behavior_without_protocol_resampling(
    setup, status, after_format_error,
):
    sessions, config, key = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        assert_request_payload(payload, system, model, config, review=False)
        calls.append(deepcopy(payload))
        if after_format_error and len(calls) == 1:
            return malformed("missing_approval")
        raise APIStatusError(PRIVATE, response=httpx.Response(
            status, request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        ), body={"private": SOURCE + CHINESE})

    service._completion = completion
    await service.translate_one(key)
    assert len(calls) == (2 if after_format_error else 1)
    with sessions() as session:
        row = session.get(Translation, key)
        assert not row.text_zh and not row.owner and not row.lease_until
        assert PRIVATE not in json.dumps(row.issues)
        if status == 402:
            assert row.status == "insufficient_balance" and row.attempts == 0 and service.balance_blocked
            assert session.get(TranslationAccountState, account_scope(config)).code == "insufficient_balance"
        else:
            assert row.status == "error" and row.attempts == 1 and not service.balance_blocked
            assert "HTTP 401" in row.issues[0]

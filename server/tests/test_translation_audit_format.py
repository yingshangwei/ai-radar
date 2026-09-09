import json
from copy import deepcopy

import httpx
import pytest
from openai import APIStatusError

from radar.config import TranslationConfig
from radar.db import database
from radar.models import Translation
from radar.translation import (
    AUDIT,
    RECHECK_POLICY,
    AuditOutput,
    TranslationService,
    TranslationValidationError,
    candidate_fingerprint,
    ensure_translation,
    part_audited,
)

SOURCE = "AI agents could use 5 tools."
CANDIDATE = "AI 智能体可能使用 5 个工具。"
PRIVATE = "private-previous-response-and-editor-approval"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/audit-format.db")
    config = TranslationConfig(enabled=True, audit_model="separate-auditor")
    with sessions.begin() as session:
        row = ensure_translation(session, SOURCE, SOURCE, config)
        key = row.id
        part = deepcopy(row.parts[0])
        part.update(draft=CANDIDATE, zh=CANDIDATE, ok=False, correction_required=False,
                    private_approval=PRIVATE, initial_draft=CANDIDATE)
        part["review"] = {
            "approved": True, "issues": [], "private_notes": PRIVATE, "policy": RECHECK_POLICY,
            "fingerprint": candidate_fingerprint(SOURCE, CANDIDATE),
        }
        row.parts = [part]
    yield sessions, config, key
    engine.dispose()


def valid_response(*, approved=True, issues=()):
    return json.dumps({"audits": [{"id": "body-0", "approved": approved, "issues": list(issues)}]})


def malformed(kind):
    result = json.loads(valid_response())
    part = result["audits"][0]
    if kind == "json":
        return '{"audits":[' + PRIVATE
    if kind == "bool_string":
        part["approved"] = "true"
    elif kind == "bool_integer":
        part["approved"] = 1
    elif kind == "issues_string":
        part["issues"] = PRIVATE
    elif kind == "issues_object":
        part["issues"] = [{"candidate": PRIVATE}]
    elif kind == "extra_field":
        part[PRIVATE] = SOURCE + CANDIDATE
    elif kind == "missing_approval":
        del part["approved"]
    else:
        raise AssertionError("Unknown fixture")
    return json.dumps(result)


def assert_clean_payload(payload, system, model):
    assert system == AUDIT and model == "separate-auditor"
    assert payload["untrusted_parts"] == [{"id": "body-0", "source": SOURCE, "candidate": CANDIDATE}]
    assert PRIVATE not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [
    "json", "bool_string", "bool_integer", "issues_string", "issues_object",
    "extra_field", "missing_approval",
])
async def test_one_format_retry_uses_unchanged_evidence_safe_feedback_and_real_audit(setup, fault):
    sessions, config, key = setup
    service, calls = TranslationService(sessions, config), []
    with sessions() as session:
        before = deepcopy(session.get(Translation, key).parts[0])

    async def completion(payload, *, system, model, stage):
        assert_clean_payload(payload, system, model)
        calls.append(deepcopy(payload))
        if len(calls) == 1:
            assert set(payload) == {"glossary", "untrusted_parts", "untrusted_document_context"}
            return malformed(fault)
        assert len(calls) == 2
        assert set(payload) == {"glossary", "untrusted_parts", "untrusted_document_context", "format_feedback"}
        assert payload["untrusted_document_context"] == calls[0]["untrusted_document_context"]
        feedback = payload["format_feedback"]
        assert set(feedback) == {"reason", "errors", "required_schema"}
        assert feedback["reason"] == "output_schema_invalid"
        assert feedback["required_schema"] == AuditOutput.model_json_schema()
        assert feedback["errors"] and all(set(error) == {"loc", "type"} for error in feedback["errors"])
        assert all(private not in json.dumps(feedback) for private in [SOURCE, CANDIDATE, PRIVATE])
        assert payload["untrusted_parts"] == calls[0]["untrusted_parts"]
        assert payload["glossary"] == calls[0]["glossary"]
        return valid_response()

    service._completion = completion
    await service.translate_one(key)

    assert len(calls) == 2
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "ready" and row.text_zh == CANDIDATE and row.original_text == SOURCE
        part = row.parts[0]
        assert part_audited(part) and part["review"] == before["review"]
        assert part["draft"] == before["draft"] and part["initial_draft"] == before["initial_draft"]
        assert [entry["kind"] for entry in part["quality_history"]] == ["audit"]
        assert row.attempts == 1 and not row.owner and not row.lease_until


@pytest.mark.asyncio
@pytest.mark.parametrize("after_format_error", [False, True])
async def test_valid_semantic_rejection_returns_immediately_without_format_resampling(setup, after_format_error):
    sessions, config, _ = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        assert_clean_payload(payload, system, model)
        calls.append(deepcopy(payload))
        if after_format_error and len(calls) == 1:
            return malformed("bool_string")
        return valid_response(approved=False, issues=["原文限定条件未准确保留"])

    service._completion = completion
    result = await service.audit([{"id": "body-0", "source": SOURCE, "candidate": CANDIDATE,
                                   "previous_approval": PRIVATE}])

    assert len(calls) == (2 if after_format_error else 1)
    assert result["body-0"].approved is False and result["body-0"].issues == ["原文限定条件未准确保留"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["json", "bool_string"])
async def test_second_invalid_output_stays_unpublished_and_keeps_draft(setup, fault, caplog):
    sessions, config, key = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        assert_clean_payload(payload, system, model)
        calls.append(deepcopy(payload))
        return malformed(fault)

    service._completion = completion
    await service.translate_one(key)

    assert len(calls) == 2
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "error" and not row.title_zh and not row.text_zh
        assert row.parts[0]["draft"] == CANDIDATE and row.parts[0]["review"]["approved"]
        assert not part_audited(row.parts[0]) and "audit" not in row.parts[0]
        assert row.attempts == 1 and not row.owner and not row.lease_until
        assert "output_schema_invalid" in row.issues[0]
        assert PRIVATE not in json.dumps(row.issues) and PRIVATE not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ids", [["unknown"], ["body-0", "body-0"]])
async def test_id_mismatch_never_triggers_format_retry_or_relabels_output(setup, bad_ids):
    sessions, config, _ = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        assert_clean_payload(payload, system, model)
        calls.append(deepcopy(payload))
        return json.dumps({"audits": [{"id": uid, "approved": True, "issues": []} for uid in bad_ids]})

    service._completion = completion
    with pytest.raises(TranslationValidationError) as error:
        await service.audit([{"id": "body-0", "source": SOURCE, "candidate": CANDIDATE}])
    assert error.value.code == "audit_part_mismatch" and len(calls) == 1
    assert "format_feedback" not in calls[0]


@pytest.mark.asyncio
async def test_balance_error_during_format_retry_preserves_standard_stop_behavior(setup):
    sessions, config, key = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        assert_clean_payload(payload, system, model)
        calls.append(deepcopy(payload))
        if len(calls) == 1:
            return malformed("bool_string")
        raise APIStatusError(PRIVATE, response=httpx.Response(
            402, request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        ), body={})

    service._completion = completion
    await service.translate_one(key)

    assert len(calls) == 2 and service.balance_blocked
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "insufficient_balance" and row.attempts == 0
        assert row.parts[0]["draft"] == CANDIDATE and not row.text_zh
        assert not row.owner and not row.lease_until

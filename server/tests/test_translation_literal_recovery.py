import json
from copy import deepcopy

import httpx
import pytest
from openai import APIStatusError
from pydantic import ValidationError
from test_translation_request_format import (
    CHINESE,
    PRIVATE,
    PROTECTED,
    SOURCE,
    assert_request_payload,
    malformed,
    translated,
)
from test_translation_request_format import setup as setup

from radar.models import Translation, TranslationAccountState
from radar.translation import (
    AUDIT,
    POLICY,
    RECHECK_POLICY,
    REVIEW,
    TranslationOutput,
    TranslationService,
    TranslationValidationError,
    account_scope,
    candidate_fingerprint,
    ensure_translation,
    part_audited,
)


def bad_literal(kind="missing"):
    zh = PROTECTED.replace("⟪原文链接-0⟫", "") if kind == "missing" else PROTECTED + "⟪原文金额-0⟫"
    return translated(zh=zh + PRIVATE)


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("kind", ["missing", "duplicate"])
async def test_literal_retry_uses_same_protected_evidence_and_only_safe_count_feedback(setup, review, kind):
    sessions, config, _ = setup
    service, calls = TranslationService(sessions, config), []
    inputs = [{"id": "body-0", "source": SOURCE}]
    if review:
        inputs[0].update(draft=CHINESE, checks=["需要核对链接完整性"])
    before = deepcopy(inputs)

    async def completion(payload, *, system, model, stage):
        assert_request_payload(payload, system, model, config, review=review)
        calls.append(deepcopy(payload))
        if len(calls) == 1:
            return bad_literal(kind)
        assert len(calls) == 2
        assert payload["untrusted_parts"] == calls[0]["untrusted_parts"]
        assert payload["glossary"] == calls[0]["glossary"]
        feedback = payload["format_feedback"]
        assert set(feedback) == {"reason", "errors", "required_schema"}
        assert feedback["reason"] == "protected_literal_mismatch"
        assert feedback["required_schema"] == TranslationOutput.model_json_schema()
        assert feedback["errors"] == [{
            "input_index": 0, "marker": "⟪原文链接-0⟫" if kind == "missing" else "⟪原文金额-0⟫",
            "expected_count": 1, "actual_count": 0 if kind == "missing" else 2,
        }]
        rendered = json.dumps(payload, ensure_ascii=False)
        assert PRIVATE not in rendered and CHINESE not in rendered
        assert "https://example.org/paper" not in rendered
        return translated()

    service._completion = completion
    result = await service.request(inputs, review=review)
    assert inputs == before and len(calls) == 2
    assert result["body-0"].zh == CHINESE


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("failures", [("schema", "literal"), ("literal", "schema"), ("literal", "literal")])
async def test_schema_and_literal_failures_share_two_completions_total(setup, review, failures):
    sessions, config, _ = setup
    service, calls = TranslationService(sessions, config), []
    inputs = [{"id": "body-0", "source": SOURCE, "draft": CHINESE}]
    before = deepcopy(inputs)

    async def completion(payload, *, system, model, stage):
        calls.append(deepcopy(payload))
        assert len(calls) <= 2, "Nested retry budget allowed an extra model invocation"
        assert payload["untrusted_parts"] == calls[0]["untrusted_parts"]
        if len(calls) == 2:
            assert payload["format_feedback"]["reason"] == (
                "output_schema_invalid" if failures[0] == "schema" else "protected_literal_mismatch"
            )
        assert PRIVATE not in json.dumps(payload)
        return malformed("extra_field") if failures[len(calls) - 1] == "schema" else bad_literal()

    service._completion = completion
    error_type = ValidationError if failures[-1] == "schema" else TranslationValidationError
    with pytest.raises(error_type) as error:
        await service.request(inputs, review=review)
    if failures[-1] == "literal":
        assert error.value.code == "protected_literal_mismatch"
    assert len(calls) == 2 and inputs == before


@pytest.mark.asyncio
async def test_all_part_counts_checked_before_restoration_and_markers_remain_part_scoped(setup, monkeypatch):
    sessions, config, _ = setup
    service, calls, parsed = TranslationService(sessions, config), [], []
    inputs = [
        {"id": "body-0", "source": "Read https://example.org/first twice: https://example.org/first"},
        {"id": "body-1", "source": "Read https://example.org/second"},
    ]
    before = deepcopy(inputs)
    parse = TranslationOutput.model_validate_json

    def capture(cls, content, *args, **kwargs):
        result = parse(content, *args, **kwargs)
        parsed.append(result)
        return result

    monkeypatch.setattr(TranslationOutput, "model_validate_json", classmethod(capture))

    async def completion(payload, *, system, model, stage):
        calls.append(deepcopy(payload))
        if len(calls) == 2:
            assert payload["format_feedback"]["errors"] == [
                {"input_index": 0, "marker": "⟪原文链接-0⟫", "expected_count": 2, "actual_count": 1}
            ]
            # The first returned part is valid, but must still be protected when
            # another part fails validation. No failed result may be half restored.
            assert parsed[0].translations[0].zh == "第二份：⟪原文链接-0⟫"
            assert parsed[0].translations[1].zh == "第一份：⟪原文链接-0⟫"
        return json.dumps({"translations": [
            {"id": "body-1", "zh": "第二份：⟪原文链接-0⟫", "approved": True, "issues": []},
            {"id": "body-0", "zh": "第一份：⟪原文链接-0⟫" + ("，⟪原文链接-0⟫" if len(calls) == 2 else ""),
             "approved": True, "issues": []},
        ]})

    service._completion = completion
    result = await service.request(inputs, review=False)
    assert len(calls) == 2 and inputs == before
    assert result["body-0"].zh.count("https://example.org/first") == 2
    assert result["body-1"].zh == "第二份：https://example.org/second"
    assert "example.org/second" not in result["body-0"].zh
    assert parsed[0].translations[0].zh == "第二份：⟪原文链接-0⟫"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unknown", "missing", "duplicate"])
async def test_id_errors_take_priority_over_missing_markers_and_never_retry(setup, fault):
    sessions, config, _ = setup
    service, calls = TranslationService(sessions, config), []
    inputs = [{"id": "body-0", "source": SOURCE}, {"id": "body-1", "source": SOURCE}]

    async def completion(payload, *, system, model, stage):
        calls.append(payload)
        ids = {"unknown": ["unknown", "body-1"], "missing": ["body-0"], "duplicate": ["body-0", "body-0"]}[fault]
        return json.dumps({"translations": [
            {"id": uid, "zh": "没有原文标记。", "approved": True, "issues": []} for uid in ids
        ]})

    service._completion = completion
    with pytest.raises(TranslationValidationError, match="翻译段落") as error:
        await service.request(inputs, review=False)
    assert error.value.code == "translation_part_mismatch" and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ready", "review_rejected", "review_issues", "audit_rejected", "audit_issues", "machine"])
async def test_literal_recovery_does_not_bypass_review_audit_or_machine_gates(setup, outcome):
    sessions, config, key = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        if system == AUDIT:
            calls.append("audit")
            assert "format_feedback" not in payload
            with sessions() as session:
                assert not session.get(Translation, key).text_zh
            return json.dumps({"audits": [{"id": "body-0", "approved": outcome != "audit_rejected",
                                           "issues": ["独立审计仍有语义问题"] if outcome == "audit_issues" else []}]})
        review = system == POLICY + REVIEW
        calls.append("review" if review else "draft")
        assert PRIVATE not in json.dumps(payload)
        if calls == ["draft"]:
            return bad_literal()
        if calls == ["draft", "draft"]:
            assert payload["format_feedback"]["reason"] == "protected_literal_mismatch"
        else:
            assert "format_feedback" not in payload
        return translated(approved=not review or outcome != "review_rejected",
                          issues=["校对仍有语义问题"] if review and outcome == "review_issues" else [],
                          zh=PROTECTED + (" 新增数字 999。" if outcome == "machine" else ""))

    service._completion = completion
    await service.translate_one(key)
    expected = ["draft", "draft", "review", "audit"]
    if outcome != "ready":
        expected += ["review"]  # Unchanged rejected candidate must not get a second audit.
    assert calls == expected
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.original_text == SOURCE and row.attempts == 1 and not row.owner and not row.lease_until
        if outcome == "ready":
            assert row.status == "ready" and row.text_zh == CHINESE and part_audited(row.parts[0])
        else:
            assert row.status == "review_required" and not row.text_zh and not row.title_zh and row.issues
            assert row.parts[0]["review"]["round"] == config.review_max_rounds
        assert PRIVATE not in json.dumps(row.parts) and PRIVATE not in json.dumps(row.issues)


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [False, True])
async def test_http_402_after_literal_error_stops_and_preserves_balance_and_saved_progress(setup, review, caplog):
    sessions, config, key = setup
    service, target_calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        is_review = system == POLICY + REVIEW
        if review and not is_review:
            return translated()
        target_calls.append(deepcopy(payload))
        if len(target_calls) == 1:
            return bad_literal()
        assert len(target_calls) == 2
        assert payload["format_feedback"]["reason"] == "protected_literal_mismatch"
        raise APIStatusError(PRIVATE, response=httpx.Response(
            402, request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        ), body={"private": SOURCE + CHINESE})

    service._completion = completion
    await service.translate_one(key)
    assert len(target_calls) == 2 and service.balance_blocked
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "insufficient_balance" and row.attempts == 0
        assert not row.owner and not row.lease_until and not row.text_zh
        assert session.get(TranslationAccountState, account_scope(config)).code == "insufficient_balance"
        assert PRIVATE not in json.dumps(row.issues) and PRIVATE not in json.dumps(row.parts)
        assert row.parts[0].get("draft") == (CHINESE if review else None)
    assert PRIVATE not in caplog.text and CHINESE not in caplog.text


@pytest.mark.asyncio
async def test_exhausted_literal_recovery_preserves_prior_candidate_and_passing_part(setup, caplog):
    sessions, config, _ = setup
    other_source, other_zh = "AI supports research.", "人工智能支持研究。"
    combined = SOURCE + "\n" + other_source
    passing = {"id": "body-1", "source": other_source, "draft": other_zh, "zh": other_zh,
               "ok": True, "correction_required": False, "issues": [],
               "audit": {"approved": True, "issues": [], "policy": RECHECK_POLICY,
                         "fingerprint": candidate_fingerprint(other_source, other_zh)}}
    with sessions.begin() as session:
        row = ensure_translation(session, combined, combined, config)
        row.parts = [{"id": "body-0", "source": SOURCE, "draft": CHINESE, "zh": CHINESE,
                      "ok": False, "correction_required": True}, deepcopy(passing)]
        key = row.id
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model, stage):
        calls.append(payload)
        assert system == POLICY + REVIEW
        assert [part["id"] for part in payload["untrusted_parts"]] == ["body-0"]
        return bad_literal()

    service._completion = completion
    await service.translate_one(key, force=True)
    assert len(calls) == 2
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "error" and row.original_text == combined
        assert not row.text_zh and not row.owner and not row.lease_until
        assert row.parts[0]["draft"] == row.parts[0]["zh"] == CHINESE
        assert "review" not in row.parts[0] and "audit" not in row.parts[0]
        assert row.parts[1] == passing
        assert "protected_literal_mismatch" in row.issues[0]
        assert PRIVATE not in json.dumps(row.parts)
    assert PRIVATE not in caplog.text and CHINESE not in caplog.text

import json
from copy import deepcopy

import pytest

from radar.config import TranslationConfig
from radar.db import database
from radar.models import Translation
from radar.translation import AUDIT, TranslationService, ensure_translation, part_audited

SOURCE = "AI agents could use 5 tools."
CANDIDATE = "AI 智能体可能使用 5 个工具。"
AUDIT_ISSUES = [f"独立审计待核对事项 {index}" for index in range(35)]
CORRECTION_ISSUES = [f"修订校对待核对事项 {index}" for index in range(17)]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/issue-capacity.db")
    config = TranslationConfig(enabled=True, audit_model="separate-auditor", review_max_rounds=2)
    with sessions.begin() as session:
        row = ensure_translation(session, SOURCE, SOURCE, config)
        key = row.id
        part = deepcopy(row.parts[0])
        part.update(draft=CANDIDATE, zh=CANDIDATE, initial_draft=CANDIDATE,
                    correction_required=False, ok=False)
        row.parts = [part]
    yield sessions, config, key
    engine.dispose()


def audit_response(*, approved=True, issues=()):
    return json.dumps({"audits": [{"id": "body-0", "approved": approved, "issues": list(issues)}]})


def correction_response(*, approved=True, issues=()):
    return json.dumps({"translations": [{
        "id": "body-0", "zh": CANDIDATE, "approved": approved, "issues": list(issues),
    }]})


@pytest.mark.asyncio
async def test_all_audit_issues_reach_correction_without_format_retry_and_remain_in_history(setup):
    sessions, config, key = setup
    service, calls = TranslationService(sessions, config), []

    async def completion(payload, *, system, model):
        assert "format_feedback" not in payload
        part = payload["untrusted_parts"][0]
        assert part["id"] == "body-0" and part["source"] == SOURCE
        if system == AUDIT:
            assert model == "separate-auditor" and part["candidate"] == CANDIDATE
            calls.append("audit")
            return audit_response(approved=False, issues=AUDIT_ISSUES) if len(calls) == 1 else audit_response()
        calls.append("correction")
        assert model == config.review_model and part["draft"] == CANDIDATE
        assert part["checks"] == AUDIT_ISSUES
        with sessions() as session:
            saved = session.get(Translation, key)
            assert saved.parts[0]["audit"]["issues"] == AUDIT_ISSUES
            assert saved.parts[0]["issues"] == AUDIT_ISSUES
        return correction_response()

    service._completion = completion
    await service.translate_one(key)

    assert calls == ["audit", "correction", "audit"]
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "ready" and row.text_zh == CANDIDATE and row.original_text == SOURCE
        part = row.parts[0]
        assert part_audited(part) and row.issues == []
        assert [entry["kind"] for entry in part["quality_history"]] == ["audit", "correction", "audit"]
        assert part["quality_history"][0]["issues"] == AUDIT_ISSUES
        assert not part["quality_history"][0]["approved"]


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_large_issue_lists_remain_rejected_and_all_repairs_are_bounded(setup, approved):
    sessions, config, key = setup
    service, calls = TranslationService(sessions, config), []
    correction_checks = []

    async def completion(payload, *, system, model):
        assert "format_feedback" not in payload
        part = payload["untrusted_parts"][0]
        assert part["source"] == SOURCE
        if system == AUDIT:
            calls.append("audit")
            assert part["candidate"] == CANDIDATE
            return audit_response(approved=approved, issues=AUDIT_ISSUES)
        calls.append("correction")
        correction_checks.append(part["checks"])
        assert all(issue in part["checks"] for issue in AUDIT_ISSUES)
        return correction_response(approved=approved, issues=CORRECTION_ISSUES)

    service._completion = completion
    await service.translate_one(key)

    assert calls == ["audit", "correction", "audit", "correction", "audit"]
    assert len(correction_checks) == config.review_max_rounds == 2
    assert correction_checks[0] == AUDIT_ISSUES
    assert correction_checks[1] == CORRECTION_ISSUES + AUDIT_ISSUES
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "review_required" and row.original_text == SOURCE
        assert not row.text_zh and not row.title_zh and not row.owner and not row.lease_until
        part = row.parts[0]
        assert part["draft"] == CANDIDATE and not part_audited(part)
        assert part["audit"]["approved"] is approved and part["review"]["approved"] is approved
        assert part["review"]["round"] == 2
        assert part["review"]["issues"] == CORRECTION_ISSUES
        assert part["audit"]["issues"] == AUDIT_ISSUES
        assert part["issues"] == CORRECTION_ISSUES + AUDIT_ISSUES
        assert row.issues == (CORRECTION_ISSUES + AUDIT_ISSUES)[:30]
        for entry in part["quality_history"]:
            assert entry["issues"] == (AUDIT_ISSUES if entry["kind"] == "audit" else CORRECTION_ISSUES)


@pytest.mark.asyncio
async def test_oversized_provider_response_still_fails_before_parsing_or_format_retry(setup, respx_mock):
    sessions, config, key = setup
    # Exercise the real SDK transport and response-length check with a mocked HTTP response.
    content = audit_response(approved=False, issues=["x" * 180001])
    route = respx_mock.post("https://api.deepseek.com/chat/completions").respond(200, json={
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": content,
        }}],
    })
    service = TranslationService(sessions, config)

    await service.translate_one(key)

    assert route.call_count == 1
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "error" and not row.text_zh and not row.title_zh
        assert "output_empty_or_oversized" in row.issues[0]
        assert row.parts[0]["draft"] == CANDIDATE and "audit" not in row.parts[0]
        assert not row.owner and not row.lease_until

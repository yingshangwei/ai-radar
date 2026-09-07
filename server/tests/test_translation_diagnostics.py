import asyncio
import json
import logging

import httpx
import pytest
from openai import APIStatusError

from radar.config import TranslationConfig
from radar.db import database
from radar.models import Translation, TranslationAccountState
from radar.translation import (
    AuditOutput,
    TranslatedPart,
    TranslationService,
    TranslationValidationError,
    account_scope,
    ensure_translation,
    failure_diagnostic,
)

SECRET = "sk-test-hidden-provider-source-candidate"
SOURCE = "AI agents can use tools."
CHINESE = "智能体可以使用工具。"


class UnprintableError(RuntimeError):
    def __str__(self):
        raise AssertionError("Raw exceptions must not be formatted")

    def __repr__(self):
        raise AssertionError("Raw exceptions must not be represented")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    engine, sessions = database(f"sqlite:///{tmp_path}/diagnostics.db")
    config = TranslationConfig(enabled=True)
    with sessions.begin() as session:
        key = ensure_translation(session, SOURCE, SOURCE, config).id
    yield sessions, config, key
    engine.dispose()


def records(caplog):
    return [json.loads(record.getMessage().removeprefix("translation_failure "))
            for record in caplog.records if record.name == "radar.translation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["draft", "correction", "audit"])
async def test_failure_logs_exact_stage_and_never_formats_exception_or_text(setup, caplog, stage):
    sessions, config, key = setup
    service = TranslationService(sessions, config)

    async def request(parts, *, review):
        if stage == ("correction" if review else "draft"):
            raise UnprintableError(SECRET, SOURCE, CHINESE)
        return {p["id"]: TranslatedPart(id=p["id"], zh=CHINESE, approved=True) for p in parts}

    async def audit(parts):
        raise UnprintableError(SECRET, SOURCE, CHINESE)

    service.request, service.audit = request, audit
    with caplog.at_level(logging.WARNING, logger="radar.translation"):
        await service.translate_one(key)
    assert records(caplog) == [{
        "event": "translation_failure", "translation_id": key, "stage": stage,
        "exception_type": "UnprintableError", "http_status": None, "code": "unclassified_error",
    }]
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status == "error" and row.original_text == SOURCE
        assert "unclassified_error" in row.issues[0]
        assert not row.text_zh and not row.owner and not row.lease_until
        if stage != "draft":
            assert row.parts[0]["draft"] == CHINESE
        if stage == "audit":
            assert row.parts[0]["review"]["approved"]
        public_failure = json.dumps(row.issues, ensure_ascii=False)
    for private in [SECRET, SOURCE, CHINESE]:
        assert private not in caplog.text and private not in public_failure
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 402, 429, 503])
async def test_http_diagnostics_keep_only_status_and_preserve_balance_behavior(setup, caplog, status):
    sessions, config, key = setup
    service = TranslationService(sessions, config)

    async def request(parts, *, review):
        raise APIStatusError(
            SECRET + SOURCE, response=httpx.Response(status, request=httpx.Request(
                "POST", "https://api.deepseek.com/chat/completions?private=" + SECRET,
                headers={"Authorization": "Bearer " + SECRET},
            )), body={"error": {"message": SECRET, "candidate": CHINESE}},
        )

    service.request = request
    with caplog.at_level(logging.WARNING, logger="radar.translation"):
        await service.translate_one(key)
    diagnostic = records(caplog)[0]
    assert diagnostic["http_status"] == status and diagnostic["exception_type"] == "APIStatusError"
    assert diagnostic["code"] == ("insufficient_balance" if status == 402 else "provider_http_error")
    assert SECRET not in caplog.text and SOURCE not in caplog.text and CHINESE not in caplog.text
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.attempts == (0 if status == 402 else 1)
        if status == 402:
            assert row.status == "insufficient_balance"
            assert row.issues == ["翻译账户余额不足，已保存进度"]
            assert session.get(TranslationAccountState, account_scope(config)).code == "insufficient_balance"
            assert service.balance_blocked
        else:
            assert row.status == "error" and f"HTTP {status}" in row.issues[0]
            assert not service.balance_blocked


def test_validation_codes_are_owned_and_schema_payload_is_never_rendered():
    own = TranslationValidationError("protected_literal_mismatch")
    assert failure_diagnostic("a" * 64, "correction", own)["code"] == "protected_literal_mismatch"
    with pytest.raises(ValueError, match="Unknown translation validation code"):
        TranslationValidationError(SECRET)
    with pytest.raises(ValueError) as caught:
        AuditOutput.model_validate({"audits": [{"id": SECRET, "approved": SECRET, "candidate": CHINESE}]})
    diagnostic = failure_diagnostic("a" * 64, "audit", caught.value)
    assert diagnostic["code"] == "output_schema_invalid"
    assert SECRET not in json.dumps(diagnostic) and CHINESE not in json.dumps(diagnostic)
    generic = failure_diagnostic(SECRET, SECRET, ValueError(SECRET))
    assert generic["translation_id"] == "invalid_cache_id" and generic["stage"] == "draft"
    assert generic["code"] == "unclassified_error" and SECRET not in json.dumps(generic)


@pytest.mark.asyncio
async def test_concurrent_workers_keep_failure_stages_local(setup, caplog):
    sessions, config, first = setup
    second_source = "AI agents can read papers."
    with sessions.begin() as session:
        second = ensure_translation(session, second_source, second_source, config).id
    service = TranslationService(sessions, config)
    audit_entered = asyncio.Event()

    async def request(parts, *, review):
        if parts[0]["source"] == SOURCE:
            await audit_entered.wait()
            raise UnprintableError(SECRET)
        return {p["id"]: TranslatedPart(id=p["id"], zh="智能体可以阅读论文。", approved=True) for p in parts}

    async def audit(parts):
        audit_entered.set()
        await asyncio.sleep(0)
        raise TranslationValidationError("audit_part_mismatch")

    service.request, service.audit = request, audit
    with caplog.at_level(logging.WARNING, logger="radar.translation"):
        await asyncio.gather(service.translate_one(first), service.translate_one(second))
    assert {row["translation_id"]: row["stage"] for row in records(caplog)} == {first: "draft", second: "audit"}
    assert {row["code"] for row in records(caplog)} == {"unclassified_error", "audit_part_mismatch"}

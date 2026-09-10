import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from radar import usage
from radar.api import create_app
from radar.config import Settings


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_USAGE_DATABASE_PATH", str(tmp_path / "usage.db"))
    return usage.store()


def test_cache_reasoning_are_subsets_and_unknown_is_not_zero(ledger):
    with usage.scope("translation", "draft"):
        usage.Call("bailian", "qwen").finish({"prompt_tokens": 1000, "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 800}, "completion_tokens_details": {"reasoning_tokens": 50}})
        usage.Call("bailian", "qwen").finish(outcome="error")
    result = ledger.report("all")
    assert result["totals"]["total_tokens"] == 1200
    assert result["totals"]["unknown_calls"] == 1
    assert result["totals"]["cached_tokens"] == 800
    assert result["totals"]["reasoning_tokens"] == 50
    assert result["groups"][0]["stage"] == "draft"
    assert result["recent"][0]["total_tokens"] is None
    assert usage.normalize({"input_tokens": 100, "output_tokens": 20,
        "cache_read_input_tokens": 80, "cache_creation_input_tokens": 40}, "anthropic")["input_tokens"] == 220
    assert usage.normalize({"prompt_tokens": True, "completion_tokens": -1}, "openai")["input_tokens"] is None


async def test_sdk_retry_usage_recorded_before_validation_and_no_content_saved(ledger, respx_mock):
    route = respx_mock.post("https://api.example.test/v1/chat/completions").mock(side_effect=[
        httpx.Response(429, json={"error": {"message": "private-secret-body", "type": "rate_limit"}}),
        httpx.Response(200, json={"id": "chat-1", "object": "chat.completion", "created": 1,
            "model": "qwen-snapshot", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "invalid-json-private-article"}}],
            "usage": {"prompt_tokens": 321, "completion_tokens": 45}})])
    with usage.scope("technical_translation", "audit"):
        async with AsyncOpenAI(api_key="test-private-key", base_url="https://api.example.test/v1", max_retries=1,
            http_client=usage.UsageHTTPClient("bailian", "qwen-plus", 10)) as client:
            response = await client.chat.completions.create(model="qwen-plus",
                messages=[{"role": "user", "content": "private-prompt"}])
            with pytest.raises(ValueError):
                json.loads(response.choices[0].message.content)
    assert route.call_count == 2
    data = ledger.report("all")
    assert data["totals"]["calls"] == 2 and data["totals"]["total_tokens"] == 366
    assert data["totals"]["unknown_calls"] == 1
    row = data["recent"][0]
    assert row["model"] == "qwen-snapshot" and row["requested_model"] == "qwen-plus"
    assert row["feature"] == "technical_translation" and row["stage"] == "audit"
    with ledger.connect() as db:
        persisted = "\n".join(db.iterdump())
    assert "private-" not in persisted and "example.test" not in persisted


async def test_timeout_receipt_and_concurrent_feature_isolation(ledger, respx_mock):
    respx_mock.post("https://api.test/v1/messages").mock(side_effect=httpx.ConnectTimeout("private-error"))

    async def call(feature):
        with usage.scope(feature, "audit"):
            async with usage.UsageHTTPClient("anthropic", "claude", 1) as client:
                with pytest.raises(httpx.ConnectTimeout):
                    await client.post("https://api.test/v1/messages", json={})

    await asyncio.gather(call("digest"), call("web_reading"))
    result = ledger.report("all")
    assert result["totals"]["unknown_calls"] == 2 and result["totals"]["total_tokens"] is None
    assert {row["feature"] for row in result["groups"]} == {"digest", "web_reading"}
    assert usage._scope.get() == ("other", "generation")


def test_codex_usage_recovery_is_idempotent_without_historical_invention(ledger):
    key = usage.session_key("/private/session", "fingerprint")
    with usage.scope("discovery_foresight"):
        usage.Call("codex", "gpt-test", key=key)
    event = b'{"type":"turn.completed","usage":{"input_tokens":2000,"cached_input_tokens":1700,"output_tokens":90,"reasoning_output_tokens":50}}\n'
    raw, model = usage.codex_receipt(event, b'model: gpt-test\nsecret: NEVERSTORE\n')
    assert model == "gpt-test"
    for _ in range(3):
        usage.recover_session("/private/session", "fingerprint", raw)
    usage.recover_session("/old/session", "old", raw)
    assert ledger.report("all")["totals"]["calls"] == 1
    assert ledger.report("all")["totals"]["total_tokens"] == 2090
    usage.Call("codex", "gpt-test", key=key).finish(raw)
    assert ledger.report("all")["totals"]["total_tokens"] == 2090


def test_restart_stale_calls_time_boundaries_and_zero(ledger):
    assert ledger.report("all")["totals"]["total_tokens"] == 0
    call = usage.Call("codex")
    call.row["started_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    call.save()
    restarted = usage.UsageStore(ledger.path)
    assert restarted.report("today")["totals"]["calls"] == 0
    assert restarted.report("7d")["totals"]["unknown_calls"] == 1
    assert restarted.report("7d")["recent"][0]["outcome"] == "unknown"
    assert restarted.report("7d")["recent"][0]["model"] == "auto-unreported"
    assert ledger.path.stat().st_mode & 0o077 == 0


def test_write_failure_does_not_repeat_or_block_model_call(ledger, monkeypatch):
    def fail(_):
        raise sqlite3.OperationalError("locked")
    before = usage._failures
    monkeypatch.setattr(ledger, "write", fail)
    usage.Call("codex").finish({"input_tokens": 1, "output_tokens": 1})
    assert usage._failures == before + 2


def test_usage_api_requires_auth_and_valid_period(tmp_path, ledger):
    config = tmp_path / "config.toml"
    config.write_text('[provider]\nkind="codex"\nmodel="gpt-test"\n')
    settings = Settings(database_url="sqlite://", config_path=str(config), reader_token="test-reader",
        usage_database_path=str(ledger.path), scheduler_enabled=False)
    client = TestClient(create_app(settings))
    assert client.get("/v1/usage").status_code == 401
    headers = {"Authorization": "Bearer test-reader"}
    assert client.get("/v1/usage?period=999", headers=headers).status_code == 422
    response = client.get("/v1/usage", headers=headers)
    assert response.status_code == 200
    assert response.json()["allocation"][0]["model"] == "gpt-test"

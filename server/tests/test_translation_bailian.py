"""Provider migration, account errors and cache reuse; synthetic HTTP only."""

import json
from copy import deepcopy

import httpx
import pytest
from openai import APIStatusError

from radar.config import TranslationConfig
from radar.db import database
from radar.models import Translation
from radar.translation import (
    TranslationService,
    cache_key,
    ensure_translation,
    failure_diagnostic,
    translation_status,
    translation_vendor,
)

BASE = "https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
SOURCE = "AI agents improve work."


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-bailian-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    engine, sessions = database(f"sqlite:///{tmp_path}/bailian.db")
    yield sessions
    engine.dispose()


def config():
    return TranslationConfig(enabled=True, base_url=BASE, api_key_env="DASHSCOPE_API_KEY",
                             model="qwen3.7-flash", review_model="qwen-plus",
                             request_options={"enable_thinking": False}, concurrency=1)


def completion(request):
    payload = json.loads(json.loads(request.content)["messages"][1]["content"])
    audit = all("candidate" in p for p in payload["untrusted_parts"])
    items = [{"id": p["id"], "approved": True, "issues": []}
             | ({} if audit else {"zh": "人工智能动态" if p["id"] == "title" else "智能体改善工作。"})
             for p in payload["untrusted_parts"]]
    return httpx.Response(200, json={
        "id": "synthetic", "object": "chat.completion", "created": 0, "model": "synthetic",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({"audits" if audit else "translations": items}),
        }}],
    })


@pytest.mark.parametrize("url,expected", [
    (BASE, "bailian"), ("https://dashscope.aliyuncs.com/compatible-mode/v1", "bailian"),
    ("https://api.deepseek.com", "deepseek"),
    ("https://maas.aliyuncs.com.evil.example/v1", "openai_chat"),
    ("https://other.example/v1", "openai_chat"),
])
def test_vendor_uses_official_hostname_boundaries(url, expected):
    assert translation_vendor(url) == expected


@pytest.mark.parametrize("base,status,body,expected", [
    (BASE, 400, {"code": "Arrearage"}, "insufficient_balance"),
    (BASE, 400, {"error": {"code": "Arrearage"}}, "insufficient_balance"),
    (BASE, 400, {"code": "InvalidParameter", "message": "Arrearage"}, "provider_http_error"),
    (BASE, 400, "Arrearage", "provider_http_error"),
    (BASE, 401, {"code": "InvalidApiKey"}, "provider_http_error"),
    (BASE, 403, {"code": "AccessDenied.Unpurchased"}, "provider_http_error"),
    (BASE, 429, {"code": "Throttling"}, "provider_http_error"),
    ("https://api.deepseek.com", 400, {"code": "Arrearage"}, "provider_http_error"),
])
def test_billing_diagnostic_requires_exact_structured_vendor_error(base, status, body, expected):
    error = APIStatusError("private-error-secret", body=body, response=httpx.Response(
        status, request=httpx.Request("POST", base + "/chat/completions")))
    value = failure_diagnostic("a" * 64, "audit", error)
    assert value["code"] == expected and value["http_status"] == status
    assert "private-error-secret" not in json.dumps(value)


@pytest.mark.asyncio
async def test_bailian_arrears_persist_recover_and_reuse_approved_cache(store, respx_mock):
    cfg = config()
    with store.begin() as session:
        key = ensure_translation(session, "AI news", SOURCE, cfg).id
    route = respx_mock.post(BASE + "/chat/completions")
    route.respond(400, json={"error": {"code": "Arrearage", "message": "private-billing-secret"}})
    service = TranslationService(store, cfg)
    await service.translate_one(key)
    with store() as session:
        row = session.get(Translation, key)
        assert row.status == "insufficient_balance" and row.attempts == 0
        assert row.parts[0]["workflow_history"][-1]["outcome"] == "known_balance"
        alert = translation_status(session, cfg)["alert"]
        assert alert["title"] == "阿里云百炼 余额不足"
        assert "private-billing-secret" not in json.dumps(alert)
    assert route.call_count == 1
    route.mock(side_effect=completion)
    # The same documented account recovery path, without losing the saved call.
    await TranslationService(store, cfg).translate_one(key, force=True)
    with store() as session:
        row = session.get(Translation, key)
        assert row.status == "ready"
        parts = deepcopy(row.parts)
        assert translation_status(session, cfg)["alert"] is None
        assert any(e.get("outcome") == "known_balance" for e in parts[0]["workflow_history"])
        assert all(e["transport"] == "bailian" for p in parts for e in p["workflow_history"]
                   if e["kind"] == "request_reserved")
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [b["model"] for b in bodies] == ["qwen3.7-flash", "qwen3.7-flash", "qwen-plus", "qwen-plus"]
    assert all(b["enable_thinking"] is False and "thinking" not in b and "reasoning_effort" not in b
               and b["response_format"] == {"type": "json_object"} for b in bodies)
    await TranslationService(store, cfg).translate_one(key, force=True)
    assert route.call_count == 4
    with store() as session:
        assert session.get(Translation, key).parts == parts


@pytest.mark.asyncio
async def test_provider_change_keeps_existing_approved_translation_without_calls(store, respx_mock):
    old = TranslationConfig(enabled=True)
    with store.begin() as session:
        key = ensure_translation(session, "AI news", SOURCE, old).id
    old_route = respx_mock.post("https://api.deepseek.com/chat/completions").mock(side_effect=completion)
    await TranslationService(store, old).translate_one(key)
    with store() as session:
        row = session.get(Translation, key)
        assert row.status == "ready"
        before = deepcopy({c.name: getattr(row, c.name) for c in row.__table__.columns})
    new = config()
    assert cache_key("AI news", SOURCE, new) == key
    with store.begin() as session:
        assert ensure_translation(session, "AI news", SOURCE, new).id == key
    await TranslationService(store, new).translate_one(key)
    assert old_route.call_count == 3 and respx_mock.calls.call_count == 3
    with store() as session:
        row = session.get(Translation, key)
        assert {c.name: getattr(row, c.name) for c in row.__table__.columns} == before
        status = translation_status(session, new)
        assert status["provider"] == "bailian" and status["audit_model"] == "qwen-plus"

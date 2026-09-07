import asyncio
import json
import logging
from copy import deepcopy

import httpx
import pytest

from radar.config import TranslationConfig
from radar.db import database
from radar.translation import TranslationService, cache_key, failure_diagnostic


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    engine, sessions = database(f"sqlite:///{tmp_path}/transport.db")
    yield sessions
    engine.dispose()


def completion(content=None, *, audit=False, finish="stop"):
    if content is None:
        content = json.dumps({"audits" if audit else "translations": [
            {"id": "body-0", "approved": True, "issues": []}
            | ({} if audit else {"zh": "智能体"})
        ]})
    return {
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": content, "reasoning_content": "PRIVATE_REASONING_SECRET",
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 23, "total_tokens": 33,
                  "completion_tokens_details": {"reasoning_tokens": 17}},
    }


def stage_config(**kw):
    return TranslationConfig(enabled=True, stage_request_options={
        "correction": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "audit": {},
    }, stage_max_tokens={"correction": 32768, "audit": 16000}, **kw)


@pytest.mark.asyncio
async def test_actual_sdk_stage_bodies_and_independent_audit_evidence(store, respx_mock):
    route = respx_mock.post("https://api.deepseek.com/chat/completions")
    route.side_effect = [httpx.Response(200, json=completion()), httpx.Response(200, json=completion()),
                         httpx.Response(200, json=completion(audit=True))]
    service = TranslationService(store, stage_config(audit_model="audit-model"))
    source = {"id": "body-0", "source": "AI agents"}
    await service.request([source], review=False)
    await service.request([source | {"draft": "智能体"}], review=True)
    await service.audit([source | {"candidate": "智能体", "approved": True, "issues": ["private"],
                                 "review_history": ["private"], "reasoning_content": "private"}])
    draft, correction, audit = [json.loads(call.request.content) for call in route.calls]
    assert (draft["model"], draft["max_tokens"], draft["thinking"]) == (
        service.config.model, 12000, {"type": "disabled"})
    assert "reasoning_effort" not in draft
    assert (correction["model"], correction["max_tokens"], correction["thinking"],
            correction["reasoning_effort"]) == (service.config.review_model, 32768, {"type": "enabled"}, "high")
    assert audit["model"] == "audit-model" and audit["max_tokens"] == 16000
    assert "thinking" not in audit and "reasoning_effort" not in audit
    for body in (draft, correction, audit):
        assert body["response_format"] == {"type": "json_object"}
        assert "tools" not in body and body.get("stream", False) is False
        assert len(body["messages"]) == 2
    assert json.loads(audit["messages"][1]["content"]) == {
        "glossary": service.config.glossary,
        "untrusted_parts": [source | {"candidate": "智能体"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["schema", "literal"])
async def test_recovery_keeps_stage_body_and_two_call_budget(store, respx_mock, kind):
    route = respx_mock.post("https://api.deepseek.com/chat/completions")
    bad = "{}" if kind == "schema" else json.dumps({"translations": [
        {"id": "body-0", "approved": True, "zh": "PRIVATE_INVALID_OUTPUT", "issues": []}]})
    route.respond(200, json=completion(bad))
    service = TranslationService(store, stage_config())
    with pytest.raises(ValueError):
        await service.request([{"id": "body-0", "source": "AI https://example.org"}], review=True)
    assert route.call_count == 2
    first, second = [json.loads(c.request.content) for c in route.calls]
    assert {k: v for k, v in first.items() if k != "messages"} == {
        k: v for k, v in second.items() if k != "messages"}
    assert second["thinking"] == {"type": "enabled"} and second["max_tokens"] == 32768
    p1, p2 = [json.loads(b["messages"][1]["content"]) for b in (first, second)]
    assert p1["untrusted_parts"] == p2["untrusted_parts"]
    assert p2["format_feedback"]["reason"] == (
        "output_schema_invalid" if kind == "schema" else "protected_literal_mismatch")
    assert "PRIVATE_INVALID_OUTPUT" not in json.dumps(p2, ensure_ascii=False)


@pytest.mark.asyncio
async def test_concurrent_sdk_calls_never_mix_stage_options(store, respx_mock):
    observed = []
    entered = asyncio.Event()

    async def respond(request):
        body = json.loads(request.content)
        observed.append(body)
        if len(observed) == 3:
            entered.set()
        await asyncio.wait_for(entered.wait(), 2)
        return httpx.Response(200, json=completion(audit=body["max_tokens"] == 16000))

    respx_mock.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
    config = stage_config()
    before = deepcopy(config.model_dump())
    service = TranslationService(store, config)
    part = {"id": "body-0", "source": "AI agents", "candidate": "智能体"}
    results = await asyncio.gather(service.request([part], review=False),
                                   service.request([part], review=True), service.audit([part]))
    assert len(results) == 3 and config.model_dump() == before
    by_budget = {b["max_tokens"]: b for b in observed}
    assert by_budget[12000]["thinking"] == {"type": "disabled"}
    assert by_budget[32768]["thinking"] == {"type": "enabled"}
    assert "thinking" not in by_budget[16000]


@pytest.mark.asyncio
async def test_nested_options_are_copied_before_sdk_mutation(store, monkeypatch):
    import openai

    seen = []

    class Client:
        def __init__(self, **kwargs):
            self.chat = self.completions = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def create(self, **kwargs):
            seen.append(deepcopy(kwargs["extra_body"]))
            kwargs["extra_body"]["thinking"]["type"] = "MUTATED"
            from openai.types.chat import ChatCompletion
            return ChatCompletion.model_validate(completion())

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    config = stage_config()
    service = TranslationService(store, config)
    for _ in range(2):
        await service.request([{"id": "body-0", "source": "AI"}], review=True)
    assert seen == [{"thinking": {"type": "enabled"}, "reasoning_effort": "high"}] * 2
    assert config.stage_request_options["correction"]["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_overall_deadline_cancels_keepalive_and_closes_stream(store, respx_mock):
    class Keepalive(httpx.AsyncByteStream):
        closed = False
        chunks = 0

        async def __aiter__(self):
            while True:
                self.chunks += 1
                yield b" "
                await asyncio.sleep(0.005)

        async def aclose(self):
            self.closed = True

    stream = Keepalive()
    route = respx_mock.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/json"}, stream=stream))
    config = TranslationConfig(enabled=True).model_copy(update={"timeout_seconds": 0.1})
    service = TranslationService(store, config)
    with pytest.raises(TimeoutError) as exc:
        await asyncio.wait_for(service.request([{"id": "body-0", "source": "AI"}], review=False), 1)
    assert stream.closed and stream.chunks > 1 and route.call_count == 1
    assert failure_diagnostic("cache", "draft", exc.value)["code"] == "request_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish,content", [("stop", ""), ("length", None)])
async def test_thinking_does_not_publish_empty_or_truncated_output(store, respx_mock, finish, content):
    route = respx_mock.post("https://api.deepseek.com/chat/completions").respond(
        200, json=completion(content, finish=finish))
    with pytest.raises(ValueError):
        await TranslationService(store, stage_config()).request([{"id": "body-0", "source": "AI"}], review=True)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_completion_telemetry_never_contains_evidence_or_secrets(store, respx_mock, caplog):
    respx_mock.post("https://api.deepseek.com/chat/completions").respond(200, json=completion())
    caplog.set_level(logging.INFO, logger="radar.translation")
    config = stage_config(review_model="https://private.example/SECRET_MODEL")
    result = await TranslationService(store, config).request(
        [{"id": "body-0", "source": "PRIVATE_SOURCE"}], review=True)
    assert result["body-0"].zh == "智能体"
    logs = [r.message for r in caplog.records if r.name == "radar.translation"]
    assert len(logs) == 1 and logs[0].startswith("translation_completion ")
    data = json.loads(logs[0].split(" ", 1)[1])
    assert data["stage"] == "correction" and data["model"].startswith("sha256:")
    assert (data["input_tokens"], data["output_tokens"], data["reasoning_tokens"]) == (10, 23, 17)
    assert data["finish_reason"] == "stop" and data["elapsed_ms"] >= 0
    for secret in ("PRIVATE_REASONING_SECRET", "PRIVATE_SOURCE", "智能体", "test-only-secret",
                   "private.example", "SECRET_MODEL"):
        assert secret not in logs[0]


def test_stage_transport_settings_do_not_invalidate_passed_cache():
    default, configured = TranslationConfig(), stage_config()
    assert cache_key("AI", "AI agents", default) == cache_key("AI", "AI agents", configured)

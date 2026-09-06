import json

import httpx
import pytest

from radar.config import ProviderConfig
from radar.providers import make_provider

OUTPUT = {
    "title": "测试日报",
    "overview": "来源概览",
    "stories": [
        {
            "title": "实际消息",
            "summary": "来源摘要",
            "why_it_matters": "基于来源的判断",
            "category": "技术",
            "source_ids": ["real"],
        }
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,structured", [("openai", True), ("openai_chat", True), ("openai_chat", False), ("anthropic", True)]
)
async def test_sdk_adapters_share_grounded_contract(kind, structured, monkeypatch, respx_mock):
    monkeypatch.setenv("TEST_MODEL_KEY", "test-only")
    text = json.dumps(OUTPUT, ensure_ascii=False)
    if kind == "openai":
        path = "responses"
        body = {
            "id": "resp_1",
            "object": "response",
            "created_at": 1788652800,
            "model": "configured-model",
            "status": "completed",
            "output": [
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ],
        }
    elif kind == "openai_chat":
        path = "chat/completions"
        body = {
            "id": "chat_1",
            "object": "chat.completion",
            "created": 1788652800,
            "model": "configured-model",
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
            ],
        }
    else:
        path = "messages"
        body = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "configured-model",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 100},
        }
    route = respx_mock.post(f"https://model.example/v1/{path}").mock(
        return_value=httpx.Response(200, json=body)
    )
    config = ProviderConfig(
        kind=kind,
        model="configured-model",
        base_url="https://model.example/v1",
        api_key_env="TEST_MODEL_KEY",
        structured_outputs=structured,
    )
    result = await make_provider(config).generate([{"id": "real", "text": "Actual source"}], "2026-09-06")
    assert result.stories[0].source_ids == ["real"]
    request = json.loads(route.calls[0].request.content)
    assert request["model"] == "configured-model"
    assert "test-only" not in route.calls[0].request.content.decode()

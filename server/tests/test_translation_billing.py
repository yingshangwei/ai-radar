import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIStatusError
from sqlalchemy import select

from radar.api import create_app
from radar.config import RadarConfig, Settings, TranslationConfig
from radar.db import database
from radar.models import Translation, TranslationAccountState
from radar.pipeline import ingest
from radar.schemas import IncomingArticle
from radar.translation import TranslationService, account_scope, translation_status


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    url = f"sqlite:///{tmp_path}/billing.db"
    engine, sessions = database(url)
    config = RadarConfig(translation=TranslationConfig(enabled=True, concurrency=1, max_attempts=1))
    with sessions.begin() as s:
        for index, word in enumerate(["work", "tools", "research"]):
            ingest(
                s,
                [
                    IncomingArticle(
                        platform="x",
                        external_id=str(index),
                        title="AI news",
                        text=f"AI agents improve {word}.",
                        author="OpenAI",
                        handle="OpenAI",
                        url=f"https://x.com/OpenAI/status/{index}",
                        published_at=datetime.now(UTC),
                    )
                ],
                config,
            )
    yield config, sessions, url
    engine.dispose()


def success_response(request):
    payload = json.loads(json.loads(request.content)["messages"][1]["content"])
    translations = [
        {
            "id": p["id"],
            "zh": "人工智能动态" if p["id"] == "title" else "智能体改善工作。",
            "approved": True,
            "issues": [],
        }
        for p in payload["untrusted_parts"]
    ]
    return httpx.Response(
        200,
        json={
            "id": "test",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps({"translations": translations})},
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_402_persists_alert_stops_batch_and_recovers_without_exhausting_retries(setup, respx_mock):
    config, sessions, _ = setup
    route = respx_mock.post("https://api.deepseek.com/chat/completions")
    route.respond(402, json={"error": {"message": "Insufficient Balance private-secret-do-not-return"}})
    service = TranslationService(sessions, config.translation)
    first = await service.pending()
    assert route.call_count == 1  # remaining queued documents are not hammered with the same 402
    assert first["alert"]["code"] == "insufficient_balance"
    assert first["alert"]["title"] == "DeepSeek 余额不足"
    assert first["counts"] == {"pending": 2, "insufficient_balance": 1}
    assert "private-secret" not in json.dumps(first)
    restarted = TranslationService(sessions, config.translation)
    with sessions() as s:
        assert translation_status(s, config.translation)["alert"] == first["alert"]
        assert all(row.attempts == 0 for row in s.scalars(select(Translation)))
    await restarted.pending(force=True)
    assert route.call_count == 2
    route.mock(side_effect=success_response)
    recovered = await restarted.pending(force=True)
    assert recovered["alert"] is None and recovered["counts"] == {"ready": 3}
    assert route.call_count == 8  # draft and review for each previously unfinished document
    await restarted.pending(force=True)
    assert route.call_count == 8  # ready translations are not regenerated


@pytest.mark.asyncio
async def test_review_402_keeps_draft_and_resume_calls_review_only(setup, respx_mock):
    config, sessions, _ = setup
    route = respx_mock.post("https://api.deepseek.com/chat/completions")
    route.mock(
        side_effect=[
            success_response,
            httpx.Response(402, json={"error": {"message": "Insufficient Balance"}}),
        ]
    )
    with sessions() as s:
        key = s.scalar(select(Translation.id))
    service = TranslationService(sessions, config.translation)
    await service.translate_one(key)
    with sessions() as s:
        assert all(p.get("draft") for p in s.get(Translation, key).parts)
        assert s.get(Translation, key).status == "insufficient_balance"
    route.mock(side_effect=success_response)
    restarted = TranslationService(sessions, config.translation)
    await restarted.translate_one(key, force=True)
    assert route.call_count == 3
    assert json.loads(route.calls[-1].request.content)["model"] == config.translation.review_model
    with sessions() as s:
        assert s.get(Translation, key).status == "ready"
        assert translation_status(s, config.translation)["alert"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 429, 500])
async def test_auth_rate_limit_and_server_errors_are_not_balance_alerts(setup, code):
    config, sessions, _ = setup
    service = TranslationService(sessions, config.translation)

    async def fail(*args, **kwargs):
        response = httpx.Response(
            code, request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        )
        raise APIStatusError(
            "Untrusted error may even say Insufficient Balance", response=response, body=None
        )

    service.request = fail
    result = await service.pending()
    assert result["alert"] is None and result["counts"] == {"error": 3}


@pytest.mark.asyncio
async def test_older_inflight_success_cannot_hide_newer_402(setup, respx_mock):
    config, sessions, _ = setup

    def later_failure(request):
        with sessions.begin() as s:
            s.add(
                TranslationAccountState(
                    id=account_scope(config.translation),
                    code="insufficient_balance",
                    observed_at=datetime.now(UTC).isoformat(),
                )
            )
        return success_response(request)

    respx_mock.post("https://api.deepseek.com/chat/completions").mock(side_effect=later_failure)
    await TranslationService(sessions, config.translation).request(
        [{"id": "title", "source": "AI news"}], review=False
    )
    with sessions() as s:
        assert translation_status(s, config.translation)["alert"] is not None
        other = config.translation.model_copy(update={"base_url": "https://another-provider.example/v1"})
        assert translation_status(s, other)["alert"] is None


@pytest.mark.asyncio
async def test_mobile_status_keeps_alert_across_restart_without_exposing_provider_error(
    setup, tmp_path, respx_mock
):
    config, sessions, url = setup
    respx_mock.post("https://api.deepseek.com/chat/completions").respond(
        402, json={"error": {"message": "secret-token"}}
    )
    await TranslationService(sessions, config.translation).pending()
    path = tmp_path / "config.toml"
    path.write_text("[translation]\nenabled=true\n")
    app = create_app(Settings(database_url=url, config_path=str(path), reader_token="reader"))
    with TestClient(app) as client:
        assert client.get("/v1/status").status_code == 401
        state = client.get("/v1/status", headers={"Authorization": "Bearer reader"})
        assert state.json()["translation"]["alert"]["code"] == "insufficient_balance"
        assert "secret-token" not in state.text
        data = client.get("/v1/articles", headers={"Authorization": "Bearer reader"}).json()
        assert data["total"] == 3 and all(a["text"] for a in data["items"])
        assert {a["translation"]["status"] for a in data["items"]} == {"pending", "insufficient_balance"}
        assert respx_mock.calls.call_count == 1  # opening the phone does not call the model

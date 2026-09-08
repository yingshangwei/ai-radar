"""Publication regressions through the real SDK and API, with synthetic model replies."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.api import create_app
from radar.config import Settings
from radar.db import database
from radar.models import Article, ArticleTranslation, Translation
from radar.pipeline import ingest
from radar.schemas import IncomingArticle
from radar.translation import TranslationService, queue_article

SOURCE = "This AI model is not open source."
BAD = "这款人工智能模型已经开源。"
GOOD = "这款人工智能模型并未开源。"


@pytest.fixture
def publication_case(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-only-key")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[translation]\nenabled=true\nconcurrency=1\nreview_max_rounds=2\n"
        "[reading]\nenabled=false\n"
    )
    settings = Settings(config_path=str(config_path), database_url=f"sqlite:///{tmp_path}/publication.db",
                        reader_token="synthetic-reader", admin_token="synthetic-admin",
                        scheduler_enabled=False, _env_file=None)
    config = settings.load()
    engine, sessions = database(settings.database_url)
    with sessions.begin() as session:
        ingest(session, [IncomingArticle(
            platform="web", external_id="synthetic-not-open-source", title=SOURCE, text=SOURCE,
            url="https://example.invalid/synthetic-model", author="Synthetic fixture",
            published_at=datetime.now(UTC),
        )], config)
        article = session.scalar(select(Article))
        assert article is not None
        queue_article(session, article, config.translation)
        session.flush()
        key = session.get(ArticleTranslation, article.id).translation_id
    yield sessions, config.translation, key, settings
    engine.dispose()


def completion(data):
    return httpx.Response(200, json={
        "id": "synthetic-completion", "object": "chat.completion", "created": 0, "model": "synthetic",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(data, ensure_ascii=False),
        }}],
    })


def visible(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/articles", headers={"Authorization": "Bearer synthetic-reader"})
        assert response.status_code == 200
        assert response.json()["total"] == 1
        return response.json()["items"][0]


@pytest.mark.asyncio
async def test_rejected_same_candidate_cannot_pass_by_sampling_a_different_verdict(publication_case, respx_mock):
    sessions, config, key, settings = publication_case
    audited_candidates = []

    def reply(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        parts = payload["untrusted_parts"]
        if all("candidate" in part for part in parts):
            audited_candidates.extend(part["candidate"] for part in parts)
            # The provider would wrongly approve an unchanged candidate if sampled again.
            approved = len(audited_candidates) > 1
            return completion({"audits": [dict(id=part["id"], approved=approved,
                                               issues=[] if approved else ["原文否定被改为肯定"])
                                          for part in parts]})
        return completion({"translations": [dict(id=part["id"], zh=BAD, approved=True, issues=[])
                                              for part in parts]})

    route = respx_mock.post("https://api.deepseek.com/chat/completions").mock(side_effect=reply)
    await TranslationService(sessions, config).translate_one(key)
    with sessions() as session:
        row = session.get(Translation, key)
        assert row.status != "ready" and row.text_zh == ""
        assert row.original_text == SOURCE and row.parts[0]["draft"] == BAD
    assert audited_candidates == [BAD]
    calls = route.call_count
    await TranslationService(sessions, config).translate_one(key, force=True)
    await TranslationService(sessions, config).translate_one(key, force=True, recheck=True)
    assert route.call_count == calls
    item = visible(settings)
    assert item["text"] == SOURCE and item["text_zh"] is None and item["title_zh"] is None
    assert BAD not in json.dumps(item, ensure_ascii=False)


@pytest.mark.asyncio
async def test_initial_correct_draft_may_remain_unchanged_and_pass_its_first_audit(publication_case, respx_mock):
    sessions, config, key, settings = publication_case
    stages = []

    def reply(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        parts = payload["untrusted_parts"]
        if all("candidate" in part for part in parts):
            stages.append("audit")
            assert all(part["candidate"] == GOOD for part in parts)
            return completion({"audits": [dict(id=part["id"], approved=True, issues=[]) for part in parts]})
        stages.append("correction" if any("draft" in part for part in parts) else "draft")
        return completion({"translations": [dict(id=part["id"], zh=GOOD, approved=True, issues=[])
                                              for part in parts]})

    route = respx_mock.post("https://api.deepseek.com/chat/completions").mock(side_effect=reply)
    await TranslationService(sessions, config).translate_one(key)
    assert stages == ["draft", "correction", "audit"]
    item = visible(settings)
    assert item["translation"]["status"] == "ready" and item["text_zh"] == GOOD
    assert item["text"] == SOURCE
    await TranslationService(sessions, config).translate_one(key, force=True)
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_changed_correction_receives_its_own_audit_before_publication(publication_case, respx_mock):
    sessions, config, key, settings = publication_case
    audited_candidates = []
    corrections = []

    def reply(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        parts = payload["untrusted_parts"]
        if all("candidate" in part for part in parts):
            audited_candidates.extend(part["candidate"] for part in parts)
            return completion({"audits": [
                dict(id=part["id"], approved=part["candidate"] == GOOD,
                     issues=[] if part["candidate"] == GOOD else ["原文否定被改为肯定"])
                for part in parts
            ]})
        if any("draft" in part for part in parts):
            corrections.append(parts)
        candidate = GOOD if len(corrections) == 2 else BAD
        return completion({"translations": [dict(id=part["id"], zh=candidate, approved=True, issues=[])
                                              for part in parts]})

    route = respx_mock.post("https://api.deepseek.com/chat/completions").mock(side_effect=reply)
    await TranslationService(sessions, config).translate_one(key)
    assert audited_candidates == [BAD, GOOD]
    assert "原文否定被改为肯定" in corrections[1][0]["checks"]
    item = visible(settings)
    assert item["translation"]["status"] == "ready" and item["text_zh"] == GOOD
    assert item["text"] == SOURCE
    with sessions() as session:
        part = session.get(Translation, key).parts[0]
        assert part["initial_draft"] == BAD and part["draft"] == GOOD
        audits = [entry for entry in part["quality_history"] if entry["kind"] == "audit"]
        assert [entry["approved"] for entry in audits] == [False, True]
        assert audits[0]["issues"] == ["原文否定被改为肯定"]
    await TranslationService(sessions, config).translate_one(key, force=True)
    assert route.call_count == 5

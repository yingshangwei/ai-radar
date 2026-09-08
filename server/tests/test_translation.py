import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.api import create_app
from radar.config import RadarConfig, Settings, TranslationConfig
from radar.db import database
from radar.models import Article, Digest, Translation
from radar.pipeline import ingest
from radar.schemas import IncomingArticle
from radar.translation import (
    AuditedPart,
    TranslatedPart,
    TranslationService,
    cache_key,
    ensure_translation,
    needs_translation,
    parts_for,
    present_articles,
    quality_issues,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/test.db")
    yield sessions
    engine.dispose()


def article(**kwargs):
    return IncomingArticle(
        **(
            dict(
                platform="x",
                external_id="123",
                author="OpenAI",
                handle="OpenAI",
                url="https://x.com/OpenAI/status/123",
                title="AI agents",
                text="AI agents are not human.",
                published_at=datetime.now(UTC),
            )
            | kwargs
        )
    )


def fake_service(store, config):
    service = TranslationService(store, config)
    calls = []

    async def request(parts, *, review):
        calls.append((review, parts))
        return {
            p["id"]: TranslatedPart(
                id=p["id"], zh="智能体" if p["id"] == "title" else "智能体不是人类。", approved=True
            )
            for p in parts
        }

    service.request = request

    async def audit(parts):
        calls.append(("audit", parts))
        return {p["id"]: AuditedPart(id=p["id"], approved=True) for p in parts}

    service.audit = audit
    return service, calls


@pytest.mark.asyncio
async def test_cache_survives_metrics_bookmarks_duplicates_and_readers(store):
    config = RadarConfig(translation=TranslationConfig(enabled=True))
    with store.begin() as s:
        ingest(s, [article(), article(external_id="456")], config)
        first = s.scalar(select(Article))
        first.saved = True
        uid = first.id
    service, calls = fake_service(store, config.translation)
    await service.pending()
    assert len(calls) == 3  # shared draft, correction, and independent audit; not per article
    with store.begin() as s:
        ingest(s, [article(metrics={"like_count": 999})], config)
        assert s.get(Article, uid).saved
        for _ in range(3):
            rendered = present_articles(s, s.scalars(select(Article)), config.translation)
            assert all(a["text_zh"] == "智能体不是人类。" for a in rendered)
            assert all(a["text"] == "AI agents are not human." for a in rendered)
    await service.pending(force=True)
    await service.evidence([{"title": article().title, "text": article().text}])
    assert len(calls) == 3
    with store.begin() as s:
        ingest(s, [article(text="AI agents can use tools.")], config)
        changed = next(
            a
            for a in present_articles(s, s.scalars(select(Article)), config.translation)
            if a["external_id"] == "123"
        )
        assert changed["text_zh"] is None  # never serve a translation of old source content
    await service.pending()
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_failed_review_resumes_draft_and_concurrent_workers_deduplicate(store):
    config = TranslationConfig(enabled=True)
    with store.begin() as s:
        key = ensure_translation(s, article().title, article().text, config).id
    service, calls = fake_service(store, config)
    good = service.request

    async def flaky(parts, *, review):
        if review:
            raise httpx.ConnectError("private-key-never-expose")
        return await good(parts, review=review)

    service.request = flaky
    await service.translate_one(key)
    with store() as s:
        row = s.get(Translation, key)
        assert row.status == "error" and all(p.get("draft") for p in row.parts)
        assert "private" not in str(row.issues)
    await service.translate_one(key)
    assert len(calls) == 1  # backoff
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(parts, *, review):
        entered.set()
        await release.wait()
        return await good(parts, review=review)

    service.request = slow
    first = asyncio.create_task(service.translate_one(key, force=True))
    await asyncio.wait_for(entered.wait(), timeout=5)
    other, other_calls = fake_service(store, config)
    await other.translate_one(key, force=True)
    release.set()
    await first
    assert not other_calls and [r for r, _ in calls] == [False, True, "audit"]
    with store() as s:
        assert s.get(Translation, key).status == "ready"


@pytest.mark.asyncio
async def test_suspect_numbers_and_semantics_are_withheld(store):
    config = TranslationConfig(enabled=True)
    source = "AI agents reduce latency by 30%, not 50%. @OpenAI https://example.com/1"
    with store.begin() as s:
        key = ensure_translation(s, source, source, config).id
    service = TranslationService(store, config)

    async def audit(parts):
        return {p["id"]: AuditedPart(id=p["id"], approved=True) for p in parts}

    service.audit = audit

    async def wrong(parts, *, review):
        return {p["id"]: TranslatedPart(id=p["id"], zh="智能体降低延迟 50%。", approved=True) for p in parts}

    service.request = wrong
    await service.translate_one(key)
    with store() as s:
        row = s.get(Translation, key)
        assert row.status == "review_required" and not row.text_zh
        assert "数字或版本不一致" in row.issues and "引用账号不一致" in row.issues

    async def unapproved(parts, *, review):
        return {
            p["id"]: TranslatedPart(
                id=p["id"],
                zh="智能体降低延迟 30%，不是 50%。 @OpenAI https://example.com/1",
                approved=False,
                issues=["句意仍需确认"],
            )
            for p in parts
        }

    service.request = unapproved
    await service.translate_one(key, force=True)
    with store() as s:
        assert s.get(Translation, key).status == "review_required"


def test_parts_preserve_long_content_and_policy_invalidation():
    source = "AI agents are not human.\n" * 900
    parts = parts_for("A title", source)
    assert len(parts) > 5
    assert " ".join(" ".join(p["source"] for p in parts[1:]).split()) == " ".join(source.split())
    assert len(parts_for(source, source)) == len(parts) - 1
    assert all(p["id"] != "title" for p in parts_for(source[:180], source))
    assert needs_translation("OpenAI says this is not a proof.")
    assert not needs_translation("OpenAI 发布模型，支持 API。")
    assert needs_translation("引用：This is not a proof.")
    config = TranslationConfig()
    assert cache_key("a", source, config) != cache_key(
        "a", source, config.model_copy(update={"revision": "zh-v2"})
    )
    assert quality_issues("Model v1.2 costs $20 at 50%.", "模型 v1.2 价格 $20，比例为 50%。") == []
    assert quality_issues("AI reached 4,000 teams.", "AI 覆盖 4000 支队伍。") == []
    assert quality_issues("AI reached 4,000 teams.", "AI 覆盖 400 支队伍。")
    assert quality_issues("In June, 13 million lines of AI code.", "六月，1300 万行 AI 代码。") == []
    assert quality_issues("AI investment: $1 billion.", "AI 投资：10 亿美元。") == []
    assert quality_issues("AI investment: $1 billion.", "AI 投资：1 亿美元。")
    assert quality_issues("AI ranked 8th.", "AI 排名第八。") == []
    assert quality_issues("A mesh transformer model.", "一种网格变压器模型。") == ["Transformer 架构术语误译"]
    assert quality_issues("An electric transformer.", "一种电力变压器。") == []
    assert quality_issues("AI source [paper](https://example.org/v3). Results: 3%.",
                          "人工智能来源[论文](https://example.org/v3)，结果为 3%。") == []
    assert quality_issues("See https://example.org/x?cost=5 for 3 models.",
                          "参见 https://example.org/x?cost=5，涵盖 3 个模型。") == []
    assert quality_issues("See https://example.org/x for 3 models.",
                          "参见 https://example.org/x，涵盖 4 个模型。") == ["数字或版本不一致"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,finish",
    [
        ("", "stop"),
        ('{"translations":[]}', "length"),
        ('{"translations":[{"id":"wrong","zh":"中文","approved":true}]}', "stop"),
        ('{"translations":[{"id":"title","zh":"中文"}]}', "stop"),
        (
            '{"translations":[{"id":"title","zh":"中文","approved":true},{"id":"title","zh":"中文","approved":true}]}',
            "stop",
        ),
    ],
)
async def test_unusable_provider_output_rejected(store, respx_mock, content, finish):
    route = respx_mock.post("https://api.deepseek.com/chat/completions").respond(
        200,
        json={
            "id": "test",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}
            ],
        },
    )
    service = TranslationService(store, TranslationConfig(enabled=True))
    with pytest.raises(ValueError):
        await service.request([{"id": "title", "source": "AI agents"}], review=True)
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == service.config.review_model
    assert sent["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_auxiliary_is_only_a_hint_and_failure_is_optional(store, respx_mock):
    service = TranslationService(
        store, TranslationConfig(enabled=True, auxiliary_url="http://localhost:5000")
    )
    route = respx_mock.post("http://localhost:5000/translate")
    route.respond(200, json={"translatedText": ["一个提示"]})
    assert await service.auxiliary([{"id": "x", "source": "AI agent"}]) == {"x": "一个提示"}
    route.mock(side_effect=httpx.ConnectError("offline"))
    assert await service.auxiliary([{"id": "x", "source": "AI agent"}]) == {}


@pytest.mark.asyncio
async def test_quote_header_survives_translation_without_model_date_reformatting(store, respx_mock):
    route = respx_mock.post("https://api.deepseek.com/chat/completions").respond(
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
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "translations": [
                                    {
                                        "id": "body-0",
                                        "zh": "智能体\n⟪引用元信息-0⟫\n模型",
                                        "approved": True,
                                        "issues": [],
                                    }
                                ]
                            }
                        ),
                    },
                }
            ],
        },
    )
    header = "[引用帖：@OpenAI，2026-09-03T09:50:50.000Z]"
    service = TranslationService(store, TranslationConfig(enabled=True))
    result = await service.request(
        [{"id": "body-0", "source": "AI agents\n" + header + "\nAI models"}], review=False
    )
    assert header in result["body-0"].zh
    assert header not in json.loads(route.calls[0].request.content)["messages"][1]["content"]


def test_existing_database_api_chinese_search_and_citation_without_model_calls(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[translation]\nenabled=true\n")
    url = f"sqlite:///{tmp_path}/api.db"
    engine, sessions = database(url)
    with sessions.begin() as s:
        ingest(s, [article()], RadarConfig())
        uid = s.scalar(select(Article)).id
        s.get(Article, uid).saved = True
    engine.dispose()
    settings = Settings(config_path=str(config_path), database_url=url, reader_token="reader")
    app = create_app(settings)
    with app.state.sessions.begin() as s:
        row = s.scalar(select(Translation))
        row.title_zh, row.text_zh, row.status = "智能体", "智能体不是人类。", "ready"
        s.add(
            Digest(
                date="2026-09-06",
                title="日报",
                overview="概览",
                stories=[{"source_ids": [uid]}],
                provider="test",
                window_start="2026-09-05",
                window_end="2026-09-06",
                source_count=1,
                coverage=[],
            )
        )

    async def forbidden(*args, **kwargs):
        raise AssertionError("Read endpoints must not translate")

    monkeypatch.setattr(TranslationService, "request", forbidden)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer reader"}
        result = client.get("/v1/articles?q=不是人类", headers=headers).json()
        assert result["total"] == 1 and result["items"][0]["saved"]
        assert result["items"][0]["text"] == article().text
        assert client.get(f"/v1/articles/{uid}", headers=headers).json()["text_zh"] == "智能体不是人类。"
        assert (
            client.get("/v1/digests/latest", headers=headers).json()["sources"][0]["text_zh"]
            == "智能体不是人类。"
        )
        assert client.get("/v1/status", headers=headers).json()["translation"]["counts"] == {"ready": 1}

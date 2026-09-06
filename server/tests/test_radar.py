import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.api import create_app
from radar.config import ProviderConfig, RadarConfig, Settings
from radar.db import database
from radar.models import Article, Digest
from radar.pipeline import Pipeline, digest_window, ingest
from radar.providers import CLIProvider, validate_result
from radar.ranking import canonicalize, rank
from radar.schemas import IncomingArticle
from radar.sources import SourceUnavailable, fetch_facebook, fetch_rss, fetch_x


def item(**overrides):
    data = dict(
        platform="x",
        external_id="123",
        url="https://x.com/OpenAI/status/123",
        title="New AI agent model",
        text="An AI agent model improves developer tools.",
        author="OpenAI",
        handle="OpenAI",
        published_at=datetime.now(UTC) - timedelta(hours=2),
        metrics={"like_count": 120},
    )
    return IncomingArticle(**(data | overrides))


@pytest.fixture
def db(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/test.db")
    yield sessions
    engine.dispose()


def test_ingestion_filters_and_idempotent_update_preserves_bookmark(db):
    config = RadarConfig()
    with db.begin() as session:
        assert ingest(session, [item(), item()], config) == 1
        row = session.scalar(select(Article))
        row.saved = True
        assert ingest(session, [item(metrics={"like_count": 900})], config) == 0
        assert row.saved and row.metrics["like_count"] == 900
        assert (
            ingest(
                session, [item(external_id="old", published_at=datetime.now(UTC) - timedelta(days=9))], config
            )
            == 0
        )
        assert (
            ingest(
                session,
                [item(external_id="future", published_at=datetime.now(UTC) + timedelta(days=1))],
                config,
            )
            == 0
        )
        assert (
            ingest(
                session, [item(external_id="off", title="Lunch", text="A nice lunch at the beach")], config
            )
            == 0
        )
        assert ingest(session, [item(external_id="quiet", handle="nobody", metrics={})], config) == 0
        assert ingest(session, [item(external_id="expert", metrics={})], config) == 1


def test_date_validation_and_rank():
    with pytest.raises(ValueError):
        item(published_at=datetime.now())
    with pytest.raises(ValueError):
        item(url="javascript:alert(1)")
    with pytest.raises(ValueError):
        item(metrics={"like_count": -2})
    assert rank(item(), True, 0) > rank(item(), False, 0)
    assert rank(item(), False, 0) > rank(item(published_at=datetime.now(UTC) - timedelta(hours=40)), False, 0)
    assert (
        canonicalize("https://twitter.com/test/status/1?utm_source=foo&s=20") == "https://x.com/test/status/1"
    )


def test_local_digest_window():
    start, end = digest_window(date(2026, 9, 6), RadarConfig())
    assert start.isoformat() == "2026-09-05T00:00:00+00:00"
    assert end.isoformat() == "2026-09-06T00:00:00+00:00"
    start, end = digest_window(date(2026, 3, 8), RadarConfig(timezone="America/New_York"))
    assert (end - start).total_seconds() == 23 * 3600


@pytest.mark.asyncio
async def test_absent_social_auth_is_explicit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("FACEBOOK_ACCESS_TOKEN", raising=False)
    async with httpx.AsyncClient() as client:
        for fetch in (
            lambda: fetch_x(client, RadarConfig(), []),
            lambda: fetch_facebook(client, RadarConfig()),
        ):
            with pytest.raises(SourceUnavailable) as error:
                await fetch()
            assert error.value.status == "auth_required"


@pytest.mark.asyncio
async def test_x_pagination_and_metric_provenance(monkeypatch, respx_mock):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-only")
    route = respx_mock.get("https://api.x.com/2/tweets/search/recent")
    post = {
        "id": "1",
        "text": "AI model news",
        "author_id": "a",
        "created_at": datetime.now(UTC).isoformat(),
        "public_metrics": {"like_count": 72},
    }
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "data": [post],
                "includes": {"users": [{"id": "a", "name": "Author", "username": "author"}]},
                "meta": {"next_token": "next"},
            },
        ),
        httpx.Response(200, json={"data": [post | {"id": "2"}], "meta": {}}),
    ]
    async with httpx.AsyncClient() as client:
        items = await fetch_x(client, RadarConfig(), [])
    assert len(items) == 2 and items[0].metrics["like_count"] == 72
    assert route.calls[1].request.url.params["next_token"] == "next"
    assert "test-only" not in str(route.calls[0].request.url)


@pytest.mark.asyncio
async def test_rss_skips_undated_entries(respx_mock):
    from radar.config import FeedConfig

    feed = FeedConfig(id="rss", name="Official", url="https://example.org/feed")
    respx_mock.get(feed.url).mock(
        return_value=httpx.Response(
            200,
            text="""<rss version="2.0"><channel><title>AI</title>
    <item><title>Undated AI story</title><link>https://example.org/1</link></item>
    <item><title>AI model release</title><link>https://example.org/2</link><pubDate>Sun, 06 Sep 2026 00:00:00 GMT</pubDate><description>AI news</description></item>
    </channel></rss>""",
        )
    )
    async with httpx.AsyncClient() as client:
        items = await fetch_rss(client, feed)
    assert len(items) == 1 and items[0].url.endswith("/2")


def test_hallucinated_citation_rejected():
    result = {
        "title": "Title",
        "overview": "Overview",
        "stories": [
            {
                "title": "A",
                "summary": "B",
                "why_it_matters": "C",
                "category": "技术",
                "source_ids": ["made-up"],
            }
        ],
    }
    with pytest.raises(ValueError, match="outside"):
        validate_result(json.dumps(result), [{"id": "real"}])


@pytest.mark.asyncio
async def test_digest_no_data_does_not_fabricate(db):
    pipeline = Pipeline(db, RadarConfig(provider=ProviderConfig(kind="extractive")))
    with pytest.raises(ValueError, match="没有有效来源"):
        await pipeline.digest(date(2020, 1, 1))
    with db() as session:
        assert session.scalar(select(Digest)) is None


@pytest.mark.asyncio
async def test_digest_is_grounded_and_idempotent(db):
    config = RadarConfig(provider=ProviderConfig(kind="extractive"))
    pipeline = Pipeline(db, config)
    day = pipeline.latest_day()
    start, end = digest_window(day, config)
    with db.begin() as session:
        ingest(session, [item(published_at=end - timedelta(hours=1))], config)
    assert await pipeline.digest(day) == day.isoformat()
    assert await pipeline.digest(day) == day.isoformat()
    with db() as session:
        digest = session.get(Digest, day.isoformat())
        assert digest.provider == "extractive"
        assert session.get(Article, digest.stories[0]["source_ids"][0]) is not None


def test_api_auth_import_filters_and_bookmarks(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[provider]\nkind="extractive"\n')
    settings = Settings(
        config_path=str(config),
        database_url=f"sqlite:///{tmp_path}/api.db",
        reader_token="reader",
        admin_token="admin",
    )
    with TestClient(create_app(settings)) as client:
        reader = {"Authorization": "Bearer reader"}
        admin = {"Authorization": "Bearer admin"}
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/articles").status_code == 401
        assert client.post("/v1/admin/jobs", headers=reader).status_code == 403
        body = {"articles": [item().model_dump(mode="json")]}
        assert client.post("/v1/admin/import", json=body, headers=admin).json()["accepted"] == 1
        articles = client.get("/v1/articles?q=developer", headers=reader).json()
        assert articles["total"] == 1
        uid = articles["items"][0]["id"]
        assert client.put(f"/v1/articles/{uid}/bookmark", json={"saved": True}, headers=reader).json()[
            "saved"
        ]
        assert client.get("/v1/articles?saved=true", headers=reader).json()["total"] == 1
        assert client.get("/v1/articles?q=%25", headers=reader).json()["total"] == 0
        assert client.get("/v1/articles?topic=产品", headers=reader).json()["total"] == 1
        assert client.get("/v1/digests/latest", headers=reader).status_code == 404
        assert "admin_token" not in client.get("/v1/status", headers=reader).text


@pytest.mark.asyncio
async def test_generic_cli_adapter_without_shell(tmp_path):
    import sys

    output = {
        "title": "日报",
        "overview": "概览",
        "stories": [
            {
                "title": "消息",
                "summary": "原文",
                "why_it_matters": "意义",
                "category": "技术",
                "source_ids": ["known"],
            }
        ],
    }
    script = tmp_path / "provider.py"
    script.write_text(
        "import sys,json\np=sys.stdin.read()\nassert 'UNTRUSTED_SOURCE_DATA' in p\nprint("
        + repr(json.dumps(output))
        + ")\n"
    )
    provider = CLIProvider(ProviderConfig(kind="command", command=[sys.executable, str(script)]))
    result = await provider.generate([{"id": "known"}], "2026-09-06")
    assert result.stories[0].source_ids == ["known"]

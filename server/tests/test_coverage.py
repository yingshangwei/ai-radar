from datetime import UTC, datetime

import httpx
import pytest

from radar.config import ProviderConfig, RadarConfig
from radar.db import database
from radar.models import Digest, SourceState
from radar.pipeline import Pipeline
from radar.sources import SourceUnavailable, fetch_anthropic, fetch_x


@pytest.mark.asyncio
async def test_empty_edition_reports_incomplete_coverage(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/coverage.db")
    pipeline = Pipeline(sessions, RadarConfig(provider=ProviderConfig(kind="extractive")))
    with sessions.begin() as session:
        session.add(
            SourceState(
                id="official",
                name="Official Feed",
                platform="rss",
                status="healthy",
                message="No updates",
                last_success_at=datetime.now(UTC).isoformat(),
            )
        )
    day = pipeline.latest_day()
    await pipeline.digest(day)
    with sessions() as session:
        edition = session.get(Digest, day.isoformat())
        assert edition.provider == "no_updates"
        assert edition.source_count == 0 and edition.stories == []
        assert "不代表全网没有" in edition.overview and "Facebook" in edition.overview
    engine.dispose()


@pytest.mark.asyncio
async def test_rate_limit_stops_without_retry_storm(monkeypatch, respx_mock):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-only")
    route = respx_mock.get("https://api.x.com/2/tweets/search/recent").mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceUnavailable) as error:
            await fetch_x(client, RadarConfig(), ["OpenAI"])
    assert error.value.status == "rate_limited" and route.call_count == 1


@pytest.mark.asyncio
async def test_anthropic_uses_publisher_dates_and_visible_text(respx_mock):
    html = """<a href="/news/test"><time>Sep 1, 2026</time><h4>AI agents</h4><p>AI research preview.</p></a>
    <a href="/news/undated"><h4>AI news without a date</h4></a>"""
    respx_mock.get("https://www.anthropic.com/news").mock(return_value=httpx.Response(200, text=html))
    async with httpx.AsyncClient() as client:
        items = await fetch_anthropic(client)
    assert len(items) == 1
    assert items[0].published_precision == "date"
    assert items[0].published_at.isoformat() == "2026-09-01T00:00:00+00:00"
    assert items[0].text == "AI research preview."

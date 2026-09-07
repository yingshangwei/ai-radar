from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from radar.config import RadarConfig
from radar.db import database
from radar.models import Article, SourceState
from radar.pipeline import Pipeline
from radar.sources import SourceUnavailable, fetch_facebook


def readable_page(*, more=False):
    body = {
        "data": [{
            "id": "123_456",
            "message": "New AI model release",
            "created_time": datetime.now(UTC).isoformat(),
            "permalink_url": "https://www.facebook.com/123/posts/456",
            "from": {"name": "AI Research Page"},
            "reactions": {"summary": {"total_count": 60}},
            "comments": {"summary": {"total_count": 4}},
            "shares": {"count": 3},
        }],
    }
    if more:
        body["paging"] = {
            "next": "https://graph.facebook.com/v23.0/123/posts?after=next-page",
            "cursors": {"after": "next-page"},
        }
    return httpx.Response(200, json=body)


FAILURES = [
    (403, "auth_required"),
    (429, "rate_limited"),
    (503, "error"),
    ("timeout", "error"),
]


def failure_response(failure):
    if failure == "timeout":
        return httpx.ReadTimeout("isolated fixture timeout")
    return httpx.Response(failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", FAILURES)
@pytest.mark.parametrize("next_page", [False, True], ids=["next-facebook-page", "next-pagination-page"])
async def test_facebook_interruption_preserves_prior_posts(
    monkeypatch, respx_mock, failure, status, next_page
):
    monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", "test-only")
    route = respx_mock.get(url__regex=r"https://graph\.facebook\.com/v23\.0/(123|789)/posts")
    route.side_effect = [readable_page(more=next_page), failure_response(failure)]
    config = RadarConfig(facebook_page_ids=["123"] if next_page else ["123", "789"])

    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceUnavailable) as error:
            await fetch_facebook(client, config)

    assert route.call_count == 2  # No immediate retry after authorization, quota or HTTP failure.
    assert error.value.status == status
    assert len(error.value.partial_items) == 1
    kept = error.value.partial_items[0]
    assert kept.external_id == "123_456" and kept.handle == "123"
    assert kept.url == "https://www.facebook.com/123/posts/456"
    assert kept.text == "New AI model release"
    assert kept.metrics == {"reaction_count": 60, "comment_count": 4, "share_count": 3}
    second = route.calls[1].request
    assert second.url.path.endswith("/123/posts" if next_page else "/789/posts")
    assert second.url.params.get("after") == ("next-page" if next_page else None)
    assert "test-only" not in str(second.url)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", FAILURES)
async def test_facebook_initial_failure_has_no_partial_posts(monkeypatch, respx_mock, failure, status):
    monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", "test-only")
    route = respx_mock.get("https://graph.facebook.com/v23.0/123/posts")
    route.side_effect = [failure_response(failure)]

    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceUnavailable) as error:
            await fetch_facebook(client, RadarConfig(facebook_page_ids=["123"]))

    assert error.value.status == status
    assert error.value.partial_items == []
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_facebook_partial_posts_are_ingested_without_marking_source_healthy(
    monkeypatch, respx_mock, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", "test-only")
    route = respx_mock.get("https://graph.facebook.com/v23.0/123/posts")
    route.side_effect = [readable_page(more=True), httpx.Response(429)]
    engine, sessions = database(f"sqlite:///{tmp_path}/facebook-partial.db")
    try:
        pipeline = Pipeline(sessions, RadarConfig(anthropic_news_enabled=False, facebook_page_ids=["123"]))
        assert await pipeline.collect() == 1
        with sessions() as session:
            article = session.scalar(select(Article))
            assert article.platform == "facebook" and article.external_id == "123_456"
            source = session.get(SourceState, "facebook")
            assert source.status == "rate_limited" and source.item_count == 1
            assert "已保留" in source.message and "采集尚未完成" in source.message
            assert source.last_success_at and source.last_attempt_at
        assert route.call_count == 2
    finally:
        engine.dispose()

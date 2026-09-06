from datetime import UTC, datetime

import httpx
import pytest

from radar.config import RadarConfig
from radar.ranking import classify
from radar.sources import SourceUnavailable, fetch_x


def post(uid="main", **overrides):
    return {
        "id": uid,
        "author_id": "author",
        "created_at": "2026-09-06T09:30:08Z",
        "text": "Now available to everyone.",
        "public_metrics": {"like_count": 40},
        **overrides,
    }


@pytest.mark.asyncio
async def test_long_form_text_and_quote_context_preserve_authorship(monkeypatch, respx_mock):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-only")
    original = post(
        "quote",
        author_id="researcher",
        created_at="2026-09-03T09:50:50Z",
        text="Announcing our work…",
        note_tweet={"text": "Claude AI agents can now connect to your tools and act on a schedule."},
        public_metrics={"like_count": 999999},
    )
    main = post(referenced_tweets=[{"type": "quoted", "id": "quote"}])
    long_text = "A detailed update. " * 20 + "The Claude model completes formal verification."
    route = respx_mock.get("https://api.x.com/2/tweets/search/recent").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [main, post("long", text="A detailed update…", note_tweet={"text": long_text})],
                "includes": {
                    "users": [
                        {"id": "author", "name": "Publisher", "username": "publisher"},
                        {"id": "researcher", "name": "Researcher", "username": "researcher"},
                    ],
                    "tweets": [original],
                },
            },
        )
    )
    async with httpx.AsyncClient() as client:
        items = await fetch_x(client, RadarConfig(x_page_size=10), [])
    assert len(items) == 2  # Referenced posts are context, not separate new events.
    current, detailed = items
    assert current.title == main["text"]
    assert current.author == "Publisher" and current.handle == "publisher"
    assert current.published_at == datetime(2026, 9, 6, 9, 30, 8, tzinfo=UTC)
    assert current.metrics == {"like_count": 40}  # Do not inherit popularity from the quote.
    assert "[引用帖：@researcher，2026-09-03T09:50:50Z]" in current.text
    assert original["note_tweet"]["text"] in current.text
    assert classify(current) and classify(detailed)  # Both were lost with shortened text alone.
    assert detailed.text == long_text and detailed.title == long_text[:180]
    params = route.calls[0].request.url.params
    assert params["max_results"] == "10"
    assert {"note_tweet", "referenced_tweets"} <= set(params["tweet.fields"].split(","))
    assert "referenced_tweets.id.author_id" in params["expansions"].split(",")


@pytest.mark.asyncio
async def test_unavailable_quote_does_not_discard_readable_posts(monkeypatch, respx_mock):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-only")
    respx_mock.get("https://api.x.com/2/tweets/search/recent").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    post(text="AI model update", referenced_tweets=[{"type": "quoted", "id": "gone"}]),
                    post("empty-video", text=""),
                ],
                "errors": [{"resource_id": "gone", "title": "Not Found Error"}],
            },
        )
    )
    async with httpx.AsyncClient() as client:
        items = await fetch_x(client, RadarConfig(), [])
    assert len(items) == 1
    assert items[0].title == "AI model update"
    assert items[0].text == "AI model update\n\n[引用帖不可用，未取得原文]"


@pytest.mark.asyncio
async def test_partial_error_without_valid_posts_still_fails(monkeypatch, respx_mock):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-only")
    respx_mock.get("https://api.x.com/2/tweets/search/recent").mock(
        return_value=httpx.Response(200, json={"data": [], "errors": [{"title": "Invalid query"}]}),
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceUnavailable):
            await fetch_x(client, RadarConfig(), [])


@pytest.mark.asyncio
async def test_reply_context_cannot_turn_unrelated_text_into_ai_news(monkeypatch, respx_mock):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-only")
    respx_mock.get("https://api.x.com/2/tweets/search/recent").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [post(text="Happy birthday!", referenced_tweets=[{"type": "replied_to", "id": "ai"}])],
                "includes": {"tweets": [post("ai", text="New AI model release")]},
            },
        )
    )
    async with httpx.AsyncClient() as client:
        items = await fetch_x(client, RadarConfig(), [])
    assert items[0].text == "Happy birthday!"
    assert classify(items[0]) == []

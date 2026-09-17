from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from radar.config import RadarConfig, ReadingConfig, TranslationConfig
from radar.db import database
from radar.models import Article, XCollectionState, XDataCall
from radar.pipeline import ingest
from radar.sources import SourceUnavailable, x_page_items
from radar.x_collection import XCollector
from radar.x_costs import XCostLedger
from radar.x_data_config import XDataConfig
from radar.x_providers import TWITTERAPI_SEARCH, vendor_page
from radar.x_shadow import ACTOR, APIFY, ApifyShadow

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def config(**values):
    return RadarConfig(x_data=XDataConfig(provider="twitterapi_io", request_interval_seconds=0.05, **values),
        x_request_budget=1, x_discovery_requests=0, x_max_pages=1, min_engagement=0,
        translation=TranslationConfig(enabled=False), reading=ReadingConfig(enabled=False))


def tweet(uid="100", handle="alice", **changes):
    return {"id": uid, "text": "AI model research", "createdAt": (NOW - timedelta(minutes=30)).isoformat(),
        "likeCount": 100, "author": {"id": "1" if handle == "alice" else "2", "userName": handle,
                                     "name": handle, "followers": 2000}, **changes}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TWITTERAPI_IO_KEY", "private-vendor-key")
    monkeypatch.setenv("APIFY_API_TOKEN", "private-apify-key")
    engine, sessions = database(f"sqlite:///{tmp_path}/test.db")
    yield sessions
    engine.dispose()


async def collect(store, cfg, handles=None, callback=None):
    async with httpx.AsyncClient() as client:
        return await XCollector(store, cfg, clock=lambda: NOW).collect(client, handles or ["alice"],
            callback or (lambda session, items: ingest(session, items, cfg)))


def calls(store):
    with store() as session:
        return list(session.scalars(select(XDataCall)))


def test_long_post_quote_reply_and_bad_pagination():
    raw = tweet(note_tweet={"text": "Full original model text", "entities": {"urls": [
        {"expanded_url": "https://example.org/paper"}]}}, quoted_tweet=tweet("200", "bob"),
        inReplyToId="300", inReplyToUserId="3")
    items = x_page_items(vendor_page([raw]))
    assert items[0].handle == "alice"
    assert items[0].text.startswith("Full original model text")
    assert "引用帖：@bob" in items[0].text
    assert {r.url for r in items[0].references} >= {"https://example.org/paper", "https://x.com/i/status/300"}
    with pytest.raises(ValueError):
        vendor_page([], has_next_page=True)
    with pytest.raises(ValueError):
        vendor_page([tweet(createdAt="2026-09-17")])


async def test_incremental_provider_cursor_isolation_and_atomic_ingestion(store, respx_mock):
    cfg = config()
    with store.begin() as session:
        session.add(XCollectionState(id="watch:alice", data={"windows": [{"next_token": "official-cursor"}]}))
    route = respx_mock.get(TWITTERAPI_SEARCH).mock(side_effect=[
        httpx.Response(200, json={"tweets": [tweet()], "has_next_page": True, "next_cursor": "vendor-next"}),
        httpx.Response(200, json={"tweets": [tweet("101")], "has_next_page": False, "next_cursor": ""})])
    first = await collect(store, cfg)
    second = await collect(store, cfg)
    assert first.accepted_count == second.accepted_count == 1
    assert "since_time:" in route.calls[0].request.url.params["query"]
    assert "-filter:retweets" in route.calls[0].request.url.params["query"]
    assert route.calls[1].request.url.params["cursor"] == "vendor-next"
    assert route.calls[0].request.headers["X-API-Key"] == "private-vendor-key"
    with store() as session:
        assert session.get(XCollectionState, "watch:alice").data["windows"][0]["next_token"] == "official-cursor"
        assert session.get(XCollectionState, "twitterapi_io:watch:alice").data["completed_through"]
    assert [c.cost_microusd for c in calls(store)] == [150, 150]
    assert [c.accepted for c in calls(store)] == [1, 1]


@pytest.mark.parametrize("pagination", [{}, {"next_cursor": ""}, {"next_cursor": None}])
async def test_empty_response_is_billed_without_fake_data(store, respx_mock, pagination):
    respx_mock.get(TWITTERAPI_SEARCH).respond(200, json={"tweets": [], "has_next_page": False, **pagination})
    result = await collect(store, config())
    assert result.status == "healthy" and result.read_count == 0
    assert calls(store)[0].cost_microusd == 150
    with store() as session:
        assert session.get(XCollectionState, "twitterapi_io:watch:alice").data["completed_through"]


async def test_timeout_keeps_reservation_and_next_request_is_blocked(store, respx_mock):
    cfg = config(daily_usd=0.004)
    route = respx_mock.get(TWITTERAPI_SEARCH).mock(side_effect=httpx.ReadTimeout("private upstream details"))
    first = await collect(store, cfg)
    second = await collect(store, cfg)
    assert first.status == "error" and second.status == "budget_exhausted"
    assert route.call_count == 1
    assert calls(store)[0].cost_microusd == 3000 and calls(store)[0].status == "unknown"
    assert "private" not in first.message + second.message


async def test_storage_failure_keeps_paid_receipt_but_rolls_back_cursor(store, respx_mock):
    route = respx_mock.get(TWITTERAPI_SEARCH).respond(200, json={"tweets": [tweet()], "has_next_page": False})
    def broken(session, items):
        ingest(session, items, config())
        raise RuntimeError("database failure")
    result = await collect(store, config(), callback=broken)
    assert result.status == "error" and route.call_count == 1
    assert calls(store)[0].cost_microusd == 150 and calls(store)[0].accepted == 0
    with store() as session:
        assert not session.get(XCollectionState, "twitterapi_io:watch:alice").data.get("head_end")
        assert not list(session.scalars(select(Article)))


@pytest.mark.parametrize("body", [
    {"tweets": [], "has_next_page": True},
    {"tweets": [], "has_next_page": True, "next_cursor": None},
    {"tweets": [], "has_next_page": False, "next_cursor": 0},
    {"tweets": [tweet(createdAt=(NOW - timedelta(days=2)).isoformat())], "has_next_page": False},
    {"tweets": [tweet(author={})], "has_next_page": False},
    {"tweets": [tweet(handle="wrong_author")], "has_next_page": False},
])
async def test_bad_or_outside_window_data_cannot_advance_coverage(store, respx_mock, body):
    respx_mock.get(TWITTERAPI_SEARCH).respond(200, json=body)
    result = await collect(store, config())
    assert result.status == "error" and result.coverage["watched_fresh"] == 0
    assert calls(store)[0].cost_microusd == 150


async def test_missing_key_does_not_call_official_or_charge(store, monkeypatch):
    monkeypatch.delenv("TWITTERAPI_IO_KEY")
    with pytest.raises(SourceUnavailable, match="TwitterAPI.io"):
        await collect(store, config())
    assert not calls(store)


def test_concurrent_reservations_month_rollover_and_restart(store):
    cfg = config(daily_usd=0.004, monthly_usd=0.004)
    def attempt(_):
        try:
            return XCostLedger(store, cfg, clock=lambda: NOW).reserve("twitterapi_io", "watch:alice", 0.003)
        except SourceUnavailable:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len([x for x in pool.map(attempt, range(2)) if x]) == 1
    assert len(calls(store)) == 1
    tomorrow = XCostLedger(store, cfg, clock=lambda: NOW + timedelta(days=1))
    with pytest.raises(SourceUnavailable):
        tomorrow.reserve("twitterapi_io", "watch:alice", 0.003)
    next_month = XCostLedger(store, cfg, clock=lambda: NOW + timedelta(days=30))
    assert next_month.reserve("twitterapi_io", "watch:alice", 0.003)


async def test_apify_lost_start_response_never_restarts(store, respx_mock):
    route = respx_mock.post(f"{APIFY}/acts/{ACTOR}/runs").mock(side_effect=httpx.ReadTimeout("unknown"))
    shadow = ApifyShadow(store, config(shadow_enabled=True), clock=lambda: NOW)
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ReadTimeout):
            await shadow.collect(client, ["alice"])
        with pytest.raises(SourceUnavailable, match="结果不明"):
            await shadow.collect(client, ["alice"])
    assert route.call_count == 1 and calls(store)[0].cost_microusd == 100000


async def test_apify_reuses_run_id_counts_total_cost_and_limits_daily_launch(store, respx_mock):
    post = respx_mock.post(f"{APIFY}/acts/{ACTOR}/runs").respond(201, json={"data": {"id": "run123"}})
    get = respx_mock.get(f"{APIFY}/actor-runs/run123").respond(200, json={"data": {
        "status": "SUCCEEDED", "usageTotalUsd": 0.004, "defaultDatasetId": "dataset123"}})
    respx_mock.get(f"{APIFY}/datasets/dataset123/items").respond(200, json=[tweet()])
    shadow = ApifyShadow(store, config(shadow_enabled=True), clock=lambda: NOW)
    async with httpx.AsyncClient() as client:
        await shadow.collect(client, ["alice"])
        complete = await shadow.collect(client, ["alice"])
        await shadow.collect(client, ["alice"])
    assert complete.read_count == 1 and complete.accepted_count == 0
    assert post.call_count == get.call_count == 1
    assert post.calls[0].request.url.params["maxTotalChargeUsd"] == "0.02"
    assert calls(store)[0].cost_microusd == 6000
    assert calls(store)[0].details["comparison"]["sample_only"] == 1


async def test_late_index_overlap_reuses_article_without_duplicate_translation(store, respx_mock):
    cfg = config()
    cfg.x_head_refresh_minutes = 25
    route = respx_mock.get(TWITTERAPI_SEARCH).respond(200, json={
        "tweets": [tweet(createdAt=(NOW-timedelta(minutes=2)).isoformat())], "has_next_page": False})
    await collect(store, cfg)
    future = NOW + timedelta(minutes=31)
    route.mock(return_value=httpx.Response(200, json={"tweets": [tweet(createdAt=(NOW-timedelta(minutes=2)).isoformat())],
                                                   "has_next_page": False}))
    async with httpx.AsyncClient() as client:
        result = await XCollector(store, cfg, clock=lambda: future).collect(client, ["alice"],
            lambda session, items: ingest(session, items, cfg))
    query = route.calls[1].request.url.params["query"]
    assert f"since_time:{int((NOW-timedelta(seconds=30,minutes=5)).timestamp())}" in query
    assert result.read_count == 1 and result.accepted_count == 0
    with store() as session:
        assert len(list(session.scalars(select(Article)))) == 1


def test_saved_credentials_are_reloadable_without_process_restart(tmp_path, monkeypatch):
    from radar.x_data_config import vendor_secret
    monkeypatch.delenv("TWITTERAPI_IO_KEY", raising=False)
    path = tmp_path / "keys.env"
    cfg = config(credentials_file=str(path))
    assert not vendor_secret(cfg, "TWITTERAPI_IO_KEY")
    path.write_text("TWITTERAPI_IO_KEY=rotated-key\n")
    assert vendor_secret(cfg, "TWITTERAPI_IO_KEY") == "rotated-key"


async def test_keyword_discovery_is_throttled_without_throttling_watches(store, respx_mock):
    cfg = config()
    cfg.x_request_budget = 2
    cfg.x_discovery_requests = 1
    cfg.x_head_refresh_minutes = 25
    route = respx_mock.get(TWITTERAPI_SEARCH).respond(200, json={"tweets": [], "has_next_page": False})
    first = await collect(store, cfg)
    future = NOW + timedelta(minutes=31)
    async with httpx.AsyncClient() as client:
        second = await XCollector(store, cfg, clock=lambda: future).collect(client, ["alice"],
            lambda session, items: 0)
    assert first.request_count == 2 and second.request_count == 1
    assert all(call.request.url.params["query"].startswith("from:alice") for call in [route.calls[0], route.calls[2]])
    assert second.coverage["discovery_fresh"] and second.status == "healthy"

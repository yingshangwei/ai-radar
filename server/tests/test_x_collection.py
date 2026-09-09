from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from radar.config import RadarConfig, ReadingConfig, TranslationConfig
from radar.db import database
from radar.models import Article, XCollectionState
from radar.pipeline import ingest
from radar.sources import SourceUnavailable
from radar.x_collection import (
    CONTROL,
    DISCOVERY,
    ENDPOINT,
    MAX_WINDOWS,
    XCollector,
    normalized_handles,
    request_budgets,
    stamp,
)


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self):
        return self.now

    def advance(self, hours=2):
        self.now += timedelta(hours=hours)


def config(**overrides):
    return RadarConfig(
        **{"x_max_pages": 1, "x_page_size": 10, "min_engagement": 0, **overrides},
        translation=TranslationConfig(enabled=False), reading=ReadingConfig(enabled=False),
    )


def response(uid="100", *, next_token=None, handle="alice", errors=False):
    body = {"data": [{"id": uid, "text": "An AI model improves neural network research.",
                      "author_id": "user", "created_at": stamp(datetime.now(UTC) - timedelta(hours=1)),
                      "public_metrics": {"like_count": 100}}],
            "includes": {"users": [{"id": "user", "username": handle, "name": handle}]},
            "meta": {"newest_id": uid}}
    if next_token:
        body["meta"]["next_token"] = next_token
    if errors:
        body["errors"] = [{"detail": "Unavailable expansion"}]
    return httpx.Response(200, json=body)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-only-private-token")
    engine, sessions = database(f"sqlite:///{tmp_path}/x.db")
    yield sessions
    engine.dispose()


async def run(sessions, cfg, handles, clock, ingest_page=None):
    async with httpx.AsyncClient() as client:
        return await XCollector(sessions, cfg, clock=clock).collect(
            client, handles, ingest_page or (lambda session, items: ingest(session, items, cfg)),
        )


def state(sessions, key):
    with sessions() as session:
        return session.get(XCollectionState, key).data


def test_existing_request_budget_and_handle_validation():
    assert request_budgets(config(), 17) == (3, 1)
    assert request_budgets(config(x_max_pages=2), 17) == (6, 2)
    assert request_budgets(config(), 0) == (1, 1)
    assert request_budgets(config(x_request_budget=1), 17) == (1, 0)
    assert request_budgets(config(x_request_budget=5, x_discovery_requests=2), 17) == (5, 2)
    assert normalized_handles(["Alice", "@alice", "BOB", "evil OR from:other", "bad-name"]) == ["alice", "bob"]


@pytest.mark.asyncio
async def test_seventeen_accounts_get_fresh_heads_within_day_under_three_requests(store, respx_mock):
    clock, cfg = Clock(), config()
    handles = [f"expert{i:02}" for i in range(17)]
    requests = []

    def page(request):
        requests.append(dict(request.url.params))
        # Every account and discovery remain busy, so completing a shared query
        # cannot explain coverage. Their cursors must not starve fresh heads.
        return response(str(len(requests)), next_token="opaque-next")

    respx_mock.get(ENDPOINT).mock(side_effect=page)
    for cycle in range(9):
        result = await run(store, cfg, handles, clock)
        assert result.request_count == 3 and result.coverage["request_budget"] == 3
        assert result.status == "partial" and "请求预算已用完" in result.message
        if cycle != 8:
            clock.advance()
    heads = [item for item in requests if item["query"].startswith("from:") and "next_token" not in item]
    assert {item["query"].split()[0][5:] for item in heads} == set(handles)
    assert result.coverage["watched_fresh"] == 17
    assert all(" OR " not in item["query"] for item in heads)
    assert all("since_id" not in item for item in requests)
    assert all(item["max_results"] == "10" for item in requests)
    assert "opaque-next" not in str(result.coverage) + result.message


async def test_freshness_priority_reallocates_budget_for_28_watches_then_resumes_discovery(store, respx_mock):
    clock = Clock()
    cfg = config(x_request_budget=3, x_discovery_requests=1, x_watch_freshness_first=True)
    handles = [f"expert{i:02}" for i in range(28)]
    queries = []

    def page(request):
        queries.append(request.url.params["query"])
        return response(str(len(queries)), next_token="backfill-later")

    respx_mock.get(ENDPOINT).mock(side_effect=page)
    for cycle in range(10):
        result = await run(store, cfg, handles, clock)
        assert result.request_count <= 3
        if cycle != 9:
            clock.advance()
    assert result.coverage["watched_fresh"] == 28
    assert all(q.startswith("from:") for q in queries[:27])
    assert any(not q.startswith("from:") for q in queries[27:])
    assert state(store, "watch:expert00")["windows"]  # Backfill was retained, not silently discarded.


@pytest.mark.asyncio
async def test_page_restart_preserves_fixed_window_and_atomic_watermark(store, respx_mock):
    clock, cfg = Clock(), config(x_request_budget=1, x_discovery_requests=0)
    route = respx_mock.get(ENDPOINT).mock(side_effect=[response(next_token="page-two"), response("101")])
    first = await run(store, cfg, ["alice"], clock)
    initial = state(store, "watch:alice")
    assert first.accepted_count == 1 and not initial.get("completed_through")
    assert initial["head_end"] == initial["windows"][0]["end"]
    clock.advance()
    second = await run(store, cfg, ["alice"], clock)
    after = state(store, "watch:alice")
    assert second.accepted_count == 1 and not after["windows"]
    assert after["completed_through"] == initial["head_end"]
    p1, p2 = [dict(call.request.url.params) for call in route.calls]
    assert p2.pop("next_token") == "page-two" and p1 == p2
    with store() as session:
        assert len(list(session.scalars(select(Article)))) == 2


@pytest.mark.asyncio
async def test_due_fresh_head_bypasses_backlog_and_completed_watermark_waits(store, respx_mock):
    clock, cfg = Clock(), config(x_request_budget=1, x_discovery_requests=0)
    route = respx_mock.get(ENDPOINT).mock(side_effect=[
        response(next_token="old-page"), response("102"), response("101"),
    ])
    await run(store, cfg, ["alice"], clock)
    first_end = state(store, "watch:alice")["head_end"]
    clock.advance(24)
    await run(store, cfg, ["alice"], clock)
    data = state(store, "watch:alice")
    assert data["head_end"] > first_end and not data.get("completed_through")
    assert dict(route.calls[1].request.url.params)["start_time"] == first_end
    assert "next_token" not in route.calls[1].request.url.params
    newest_end = data["head_end"]
    clock.advance()
    await run(store, cfg, ["alice"], clock)
    assert route.calls[2].request.url.params["next_token"] == "old-page"
    assert state(store, "watch:alice")["completed_through"] == newest_end


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", [(401, "auth_required"), (402, "rate_limited"), (429, "rate_limited"), (503, "error")])
async def test_partial_success_then_failure_keeps_commits_and_stops(store, respx_mock, failure, status):
    cfg, clock = config(x_request_budget=3, x_discovery_requests=0), Clock()
    route = respx_mock.get(ENDPOINT).mock(side_effect=[response(), httpx.Response(failure, text="private-token-response")])
    result = await run(store, cfg, ["alice", "bob", "carol"], clock)
    assert result.status == status and result.request_count == route.call_count == 2
    assert result.read_count == result.accepted_count == 1
    assert result.committed_pages == 1 and "已保留" in result.message
    assert "private-token" not in result.message
    assert state(store, "watch:alice")["completed_through"]
    assert not state(store, "watch:bob").get("head_end")
    assert not state(store, CONTROL)["owner"]
    with store() as session:
        assert len(list(session.scalars(select(Article)))) == 1


@pytest.mark.asyncio
async def test_ingest_failure_rolls_back_articles_and_cursor_without_retry(store, respx_mock):
    cfg, clock = config(x_request_budget=1, x_discovery_requests=0), Clock()
    route = respx_mock.get(ENDPOINT).mock(side_effect=[response(next_token="next"), response(next_token="next")])

    def broken(session, items):
        ingest(session, items, cfg)
        raise RuntimeError("private article and credential")

    result = await run(store, cfg, ["alice"], clock, broken)
    assert result.status == "error" and result.accepted_count == result.read_count == 0
    assert "private" not in result.message and route.call_count == 1
    before = state(store, "watch:alice")
    assert not before.get("head_end") and before["windows"][0]["next_token"] == ""
    with store() as session:
        assert not list(session.scalars(select(Article)))
    clock.advance()
    result = await run(store, cfg, ["alice"], clock)
    assert result.accepted_count == 1
    assert dict(route.calls[0].request.url.params) == dict(route.calls[1].request.url.params)


@pytest.mark.asyncio
async def test_discovery_always_starts_new_head_then_only_that_head_pages(store, respx_mock):
    cfg, clock = config(x_request_budget=2, x_discovery_requests=2), Clock()
    route = respx_mock.get(ENDPOINT).mock(side_effect=[
        response("1", next_token="old-2"), response("2", next_token="old-3"),
        response("3", next_token="new-2"), response("4"),
        response("5"),
    ])
    await run(store, cfg, [], clock)
    old_end = state(store, DISCOVERY)["head_end"]
    clock.advance()
    await run(store, cfg, [], clock)
    assert "next_token" not in route.calls[2].request.url.params
    assert route.calls[2].request.url.params["start_time"] == old_end
    assert route.calls[3].request.url.params["next_token"] == "new-2"
    clock.advance()
    result = await run(store, cfg, [], clock)
    assert result.request_count == 1  # No spending the second request on old-3.
    assert state(store, DISCOVERY)["windows"][0]["next_token"] == "old-3"


@pytest.mark.asyncio
async def test_pending_windows_bounded_with_persistent_gap_and_expiry(store, respx_mock):
    cfg, clock = config(x_request_budget=1, x_discovery_requests=1), Clock()
    respx_mock.get(ENDPOINT).mock(return_value=response(next_token="more"))
    for _ in range(MAX_WINDOWS + 2):
        await run(store, cfg, [], clock)
        clock.advance()
    data = state(store, DISCOVERY)
    assert len(data["windows"]) == MAX_WINDOWS and data["gap_count"] == 2
    assert data["gaps"][-1]["reason"] == "pending_window_limit"
    clock.advance(168)
    result = await run(store, cfg, [], clock)
    data = state(store, DISCOVERY)
    assert len(data["windows"]) == 1 and data["gap_count"] >= 10
    assert result.status == "partial" and not data.get("completed_through")


@pytest.mark.asyncio
async def test_invalid_cursor_resets_only_cursor_without_extra_request(store, respx_mock):
    cfg, clock = config(x_request_budget=1, x_discovery_requests=0), Clock()
    route = respx_mock.get(ENDPOINT).mock(side_effect=[response(next_token="bad-cursor"), httpx.Response(400), response()])
    await run(store, cfg, ["alice"], clock)
    before = state(store, "watch:alice")
    clock.advance()
    result = await run(store, cfg, ["alice"], clock)
    assert result.status == "error" and result.request_count == 1
    assert state(store, "watch:alice")["windows"][0]["next_token"] == ""
    clock.advance()
    result = await run(store, cfg, ["alice"], clock)
    assert result.accepted_count == 0  # Replayed first-page item deduplicates.
    assert route.calls[2].request.url.params["end_time"] == before["head_end"]
    assert "next_token" not in route.calls[2].request.url.params


@pytest.mark.asyncio
async def test_partial_response_cannot_claim_complete_coverage(store, respx_mock):
    cfg, clock = config(x_request_budget=1, x_discovery_requests=0), Clock()
    respx_mock.get(ENDPOINT).mock(return_value=response(errors=True))
    result = await run(store, cfg, ["alice"], clock)
    data = state(store, "watch:alice")
    assert result.accepted_count == 1 and result.status == "partial"
    assert data["gap_count"] == 1 and not data.get("completed_through")


@pytest.mark.asyncio
async def test_live_collection_lease_rejects_before_http(store, respx_mock):
    clock = Clock()
    with store.begin() as session:
        session.add(XCollectionState(id=CONTROL, data={"owner": "other", "lease_until": stamp(clock() + timedelta(minutes=1))}))
    route = respx_mock.get(ENDPOINT).mock(return_value=response())
    with pytest.raises(SourceUnavailable):
        await run(store, config(), ["alice"], clock)
    assert not route.calls and state(store, CONTROL)["owner"] == "other"


@pytest.mark.asyncio
async def test_query_change_does_not_reuse_another_queries_cursor(store, respx_mock):
    cfg, clock = config(), Clock()
    route = respx_mock.get(ENDPOINT).mock(return_value=response(next_token="old-query-cursor"))
    await run(store, cfg, [], clock)
    clock.advance()
    result = await run(store, config(x_query="robotics -is:retweet"), [], clock)
    assert "next_token" not in route.calls[1].request.url.params
    assert result.coverage["gap_count"] == 1


@pytest.mark.asyncio
async def test_configured_page_size_change_does_not_change_resumed_window(store, respx_mock):
    cfg, clock = config(x_request_budget=1, x_discovery_requests=0), Clock()
    route = respx_mock.get(ENDPOINT).mock(return_value=response(next_token="next"))
    await run(store, cfg, ["alice"], clock)
    clock.advance()
    cfg.x_page_size = 100
    await run(store, cfg, ["alice"], clock)
    assert route.calls[1].request.url.params["max_results"] == "10"


@pytest.mark.asyncio
async def test_backfill_is_fair_after_all_heads_are_fresh(store, respx_mock):
    cfg, clock = config(x_request_budget=2, x_discovery_requests=0), Clock()
    queries = []

    def page(request):
        queries.append(request.url.params["query"])
        return response(str(len(queries)), next_token="more")

    respx_mock.get(ENDPOINT).mock(side_effect=page)
    for _ in range(4):
        await run(store, cfg, ["alice", "bob", "carol", "dan"], clock)
        clock.advance()
    assert Counter(queries) == Counter({f"from:{name} -is:retweet": 2 for name in ["alice", "bob", "carol", "dan"]})


@pytest.mark.asyncio
async def test_expired_prefix_keeps_searchable_suffix_with_new_cursor_scope(store, respx_mock):
    cfg, clock = config(x_request_budget=1, x_discovery_requests=0), Clock()
    route = respx_mock.get(ENDPOINT).mock(side_effect=[response(next_token="old-token"), response("101")])
    await run(store, cfg, ["alice"], clock)
    before = state(store, "watch:alice")
    original = before["windows"][0]
    # Original range is 24h long. After 150h, only its prefix has crossed the
    # 167h safe retention boundary. Directly exercise formal backlog selection.
    clock.advance(150)
    collector = XCollector(store, cfg, clock=clock)
    owner = "suffix-test-owner"
    collector.initialize(["alice"], owner)
    new_id, params = collector.prepare("watch:alice", False, owner)
    after = state(store, "watch:alice")
    assert len(after["windows"]) == 1 and new_id != original["id"]
    assert params["end_time"] == original["end"]
    assert params["start_time"] == stamp(clock() - timedelta(hours=167))
    assert "next_token" not in params
    assert after["gaps"][-1] == {
        "start": original["start"], "end": params["start_time"], "reason": "outside_recent_window",
    }
    assert not after.get("completed_through") and route.call_count == 1


@pytest.mark.asyncio
async def test_successful_empty_page_then_error_retains_committed_page_count(store, respx_mock):
    cfg, clock = config(x_request_budget=2, x_discovery_requests=0), Clock()
    respx_mock.get(ENDPOINT).mock(side_effect=[httpx.Response(200, json={"meta": {"result_count": 0}}), httpx.Response(429)])
    result = await run(store, cfg, ["alice", "bob"], clock)
    assert result.committed_pages == 1 and result.read_count == result.accepted_count == 0
    assert result.status == "rate_limited" and result.request_count == 2
    assert state(store, "watch:alice")["completed_through"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"meta": {"next_token": 42}}, {"data": {}, "meta": {}}])
async def test_malformed_success_response_does_not_advance_watermark(store, respx_mock, body):
    cfg, clock = config(x_request_budget=1, x_discovery_requests=0), Clock()
    respx_mock.get(ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    result = await run(store, cfg, ["alice"], clock)
    assert result.status == "error" and result.committed_pages == 0
    assert not state(store, "watch:alice").get("head_end")


@pytest.mark.asyncio
async def test_recent_success_for_old_retried_head_is_not_reported_fresh(store, respx_mock):
    cfg, clock = config(x_request_budget=1, x_discovery_requests=0), Clock()
    respx_mock.get(ENDPOINT).mock(side_effect=[httpx.Response(503), response()])
    await run(store, cfg, ["alice"], clock)
    clock.advance(36)
    result = await run(store, cfg, ["alice"], clock)
    assert result.committed_pages == 1 and result.coverage["watched_fresh"] == 0
    assert result.status == "partial"


@pytest.mark.asyncio
async def test_initial_short_lookback_does_not_invent_an_expired_gap(store, respx_mock):
    cfg, clock = config(x_request_budget=1, x_discovery_requests=0, lookback_hours=1), Clock()
    route = respx_mock.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"meta": {"result_count": 0}}))
    result = await run(store, cfg, ["alice"], clock)
    assert result.coverage["gap_count"] == 0
    assert route.calls[0].request.url.params["start_time"] == stamp(clock() - timedelta(hours=1))


@pytest.mark.asyncio
async def test_no_enabled_queries_does_not_claim_healthy_coverage(store, respx_mock):
    route = respx_mock.get(ENDPOINT).mock(return_value=response())
    result = await run(store, config(x_discovery_requests=0), [], Clock())
    assert result.request_count == result.committed_pages == route.call_count == 0
    assert result.status == "partial" and "未启用关注账号或广泛发现" in result.message
    assert result.coverage["enabled_scopes"] == 0 and result.coverage["scopes"] == []
    assert result.coverage["discovery_enabled"] is False


@pytest.mark.asyncio
async def test_disabled_discovery_history_is_preserved_but_not_counted_as_active_coverage(store, respx_mock):
    clock = Clock()
    route = respx_mock.get(ENDPOINT).mock(side_effect=[response(next_token="old-discovery", errors=True), response("102")])
    await run(store, config(), [], clock)
    with store.begin() as session:
        row = session.get(XCollectionState, DISCOVERY)
        row.data = {**row.data, "gap_count": 2, "gaps": [{"reason": "partial_response"}]}
    discovery_before = state(store, DISCOVERY)
    clock.advance()
    result = await run(store, config(x_request_budget=1, x_discovery_requests=0), ["alice"], clock)
    assert result.request_count == 1 and route.call_count == 2 and result.status == "healthy"
    assert result.coverage["pending_windows"] == result.coverage["gap_count"] == 0
    assert result.coverage["partial_response"] is False
    assert [scope["scope"] for scope in result.coverage["scopes"]] == ["watch:alice"]
    assert "覆盖统计仅包括关注账号" in result.message
    assert state(store, DISCOVERY) == discovery_before


@pytest.mark.asyncio
async def test_missing_quote_keeps_main_text_and_explains_conservative_partial_state(store, respx_mock):
    body = response().json()
    body["data"][0]["referenced_tweets"] = [{"type": "quoted", "id": "gone"}]
    body["errors"] = [{"resource_id": "gone", "title": "Not Found Error", "detail": "private diagnostic"}]
    respx_mock.get(ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    result = await run(store, config(x_request_budget=1, x_discovery_requests=0), ["alice"], Clock())
    assert result.status == "partial" and result.accepted_count == result.read_count == 1
    assert result.coverage["gap_count"] == 1 and result.coverage["partial_response"] is True
    assert "可能仅涉及引用或附加信息；可读主帖已保留" in result.message
    assert "正文未" not in result.message and "private diagnostic" not in result.message
    with store() as session:
        article = session.scalar(select(Article))
        assert article.text.startswith(body["data"][0]["text"])
        assert "[引用帖不可用，未取得原文]" in article.text

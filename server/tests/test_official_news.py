import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from radar.article_presentation import article_evidence
from radar.config import RadarConfig, ReadingConfig, TranslationConfig
from radar.db import database
from radar.models import Article, ArticleDocument, SourceState, Translation
from radar.official_news import NEWS_SOURCES, fetch_news, parse_news
from radar.pipeline import Pipeline, ingest
from radar.ranking import classify
from radar.sources import SourceUnavailable
from radar.summary_evidence import digest_review_evidence
from radar.x_collection import XCollectionResult


def seed_html(**changes):
    row = {"ArticleMeta": {"PublishDate": 1785859200000, "UpdateTime": 1788404941000,
                           "StatusEn": 2, "IsPinned": True},
           "ArticleSubContentEn": {"Title": "SeedRealtime Released", "Abstract": "Publisher excerpt.",
                                   "TitleKey": "seedrealtime-released"}}
    row["ArticleMeta"].update(changes)
    data = {"loaderData": {"(locale$)/blog/page": {"article_list": [row]}}}
    return '<script>window._ROUTER_DATA = ' + json.dumps(data) + '</script>'


def card(kind, date="2026-09-08", url=None):
    if kind == "deepseek":
        return (f'<a href="{url or "/en/news/new-model/"}"><p><span>{date}</span></p>'
                '<h2>DeepSeek announcement</h2><p>Original publisher excerpt.</p></a>')
    if kind == "kimi":
        return (f'<div class="menu-card"><a href="{url or "/en/blog/new-model"}"></a>'
                f'<h4>Kimi announcement</h4><p class="card-date">{date}</p></div>')
    return (f'<a href="{url or "https://www.minimax.io/blog/new-model"}"><article><time>{date}</time>'
            '<h2>MiniMax announcement</h2><p>Original publisher excerpt.</p></article></a>')


def test_seed_original_publication_date_not_update_or_pinned_position():
    item = parse_news("seed", seed_html()).items[0]
    assert item.published_at == datetime(2026, 8, 5, tzinfo=UTC)
    assert item.published_precision == "date" and item.metrics == {}
    assert item.text == "Publisher excerpt." and "Released" in item.title
    assert item.url == "https://seed.bytedance.com/en/blog/seedrealtime-released"
    assert item.external_id == parse_news("seed", seed_html(UpdateTime=1789000000000)).items[0].external_id


@pytest.mark.parametrize("changes", [{"PublishDate": None}, {"PublishDate": True}, {"StatusEn": 1}])
def test_seed_missing_original_date_and_unpublished_do_not_become_today(changes):
    with pytest.raises(ValueError):
        parse_news("seed", seed_html(**changes))


@pytest.mark.parametrize("kind", ["deepseek", "kimi", "minimax"])
@pytest.mark.parametrize("date", ["2026-09-08", "September 08, 2026", "Sep 08, 2026"])
def test_dated_cards_keep_provenance_and_deduplicate(kind, date):
    result = parse_news(kind, card(kind, date) * 2 + '<nav><a href="/news/undated">New model</a></nav>')
    assert result.status == "healthy" and len(result.items) == 1
    item = result.items[0]
    assert item.published_at == datetime(2026, 9, 8, tzinfo=UTC)
    assert item.source_id == "official-" + kind and item.author == NEWS_SOURCES[kind][0]
    assert item.published_precision == "date" and item.metrics == {}


@pytest.mark.parametrize("url", ["http://127.0.0.1/blog/test", "https://www.minimax.io.evil.test/blog/x",
                                  "https://secret@www.minimax.io/blog/x", "https://www.minimax.io/blog/x?k=1"])
def test_index_cannot_introduce_another_origin_or_credential_url(url):
    with pytest.raises(ValueError):
        parse_news("minimax", card("minimax", url=url))


def test_kimi_explicit_publisher_owned_code_destination_is_allowed_without_crawling():
    item = parse_news("kimi", card("kimi", url="https://github.com/MoonshotAI/Kimi-Dev")).items[0]
    assert item.url == "https://github.com/MoonshotAI/Kimi-Dev"
    assert item.text == item.title
    with pytest.raises(ValueError):
        parse_news("kimi", card("kimi", url="https://github.com/another-owner/Kimi-Dev"))


def test_bad_date_retains_good_items_and_reports_partial():
    r = parse_news("deepseek", card("deepseek") + card("deepseek", date="today", url="/en/news/other/"))
    assert len(r.items) == 1 and r.status == "partial"
    with pytest.raises(ValueError):
        parse_news("deepseek", "<h1>Just a moment</h1>")


async def test_fetch_is_single_bounded_index_request_with_no_auth_or_recursive_follow():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, text=card("minimax"), headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert len((await fetch_news(client, "minimax")).items) == 1
    assert len(calls) == 1 and str(calls[0].url) == NEWS_SOURCES["minimax"][1]
    assert "authorization" not in calls[0].headers


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
async def test_http_errors_do_not_become_news_or_automatic_login(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        status, text=card("deepseek"), headers={"content-type": "text/html", "location": "https://example.com"},
    ))) as client:
        with pytest.raises((httpx.HTTPStatusError, SourceUnavailable)):
            await fetch_news(client, "deepseek")


async def test_pipeline_uses_saved_translation_and_reading_flow_and_retries_failed_source(tmp_path, monkeypatch):
    engine, sessions = database(f"sqlite:///{tmp_path}/radar.db")
    try:
        config = RadarConfig(anthropic_news_enabled=False, official_news_sources=["deepseek", "kimi"],
                             translation=TranslationConfig(enabled=True), reading=ReadingConfig(enabled=True))
        date = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        result = parse_news("deepseek", card("deepseek", date))
        mock = AsyncMock(side_effect=[result, ValueError("page changed"), result, parse_news("kimi", card("kimi", date))])
        monkeypatch.setattr("radar.pipeline.fetch_news", mock)
        monkeypatch.setattr("radar.pipeline.fetch_facebook", AsyncMock(return_value=[]))
        monkeypatch.setattr("radar.pipeline.XCollector.collect", AsyncMock(return_value=XCollectionResult()))
        pipeline = Pipeline(sessions, config)
        assert await pipeline.collect() == 1
        with sessions() as s:
            assert s.get(SourceState, "official-kimi").status == "error"
            first_cache = set(s.scalars(select(Translation.id)))
            article = s.scalar(select(Article))
            assert article.priority and s.scalar(select(ArticleDocument)) is not None
            assert article_evidence(article)["partial"]
            assert digest_review_evidence(s, [{"id": article.id}], config)[0]["partial"]
        assert await pipeline.collect() == 1
        with sessions() as s:
            assert first_cache <= set(s.scalars(select(Translation.id)))
            assert s.get(SourceState, "official-kimi").status == "healthy"
            assert len(list(s.scalars(select(Article)))) == 2
        old = result.items[0].model_copy(update={"published_at": datetime.now(UTC) - timedelta(days=60)})
        with sessions.begin() as s:
            assert ingest(s, [old], config, 2.0) == 0
    finally:
        engine.dispose()


def test_chinese_model_names_classify_without_broad_seed_agriculture_match():
    item = parse_news("kimi", card("kimi")).items[0]
    for text in ["Qwen3.8 is here", "GLM-5.3", "Kimi K3", "MiniMax M3", "豆包的新进展"]:
        assert classify(item.model_copy(update={"platform": "x", "source_id": "x", "title": text, "text": text}))
    assert not classify(item.model_copy(update={"platform": "x", "source_id": "x", "title": "Seed corn", "text": "Seed corn"}))

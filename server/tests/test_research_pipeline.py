from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from radar.article_presentation import article_evidence
from radar.config import RadarConfig, ReadingConfig, ResearchConfig, TranslationConfig
from radar.db import database
from radar.models import Article, ArticleReading, SourceState, Translation
from radar.pipeline import Pipeline, ingest
from radar.research import ResearchResult
from radar.schemas import IncomingArticle
from radar.summary_evidence import digest_review_evidence
from radar.x_collection import XCollectionResult


@pytest.fixture
def store(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/papers.db")
    yield sessions
    engine.dispose()


def paper(source="arxiv-theory", **changes):
    values = dict(platform="web", external_id="arxiv:2609.01234", source_id=source,
                  url="https://arxiv.org/abs/2609.01234", title="Generalization bounds for optimization",
                  text="arXiv preprint · Author abstract\n\nWe establish a bound under bounded-gradient assumptions.",
                  author="Alice Example", published_at=datetime.now(UTC) - timedelta(hours=12),
                  metrics={"arxiv_version": 1})
    return IncomingArticle(**(values | changes))


def config(**changes):
    return RadarConfig(anthropic_news_enabled=False, translation=TranslationConfig(enabled=True),
                       reading=ReadingConfig(enabled=True), **changes)


def test_cross_source_votes_keep_versioned_abstract_and_translation_cache(store):
    cfg = config()
    original = paper(metrics={"arxiv_version": 2})
    with store.begin() as session:
        assert ingest(session, [original], cfg) == 1
        first = session.scalar(select(Article))
        first_id, source_text = first.id, first.text
        cache_ids = set(session.scalars(select(Translation.id)))
        assert first.topics[:2] == ["学界", "技术"]
    recommended = paper("hf-papers", title="Older unversioned title", text="Older abstract.",
                        metrics={"like_count": 42, "hf_upvotes": 42, "arxiv_version": 0},
                        references=[{"url": "https://github.com/example/paper"}])
    with store.begin() as session:
        assert ingest(session, [recommended], cfg, 1.5) == 0
        row = session.get(Article, first_id)
        assert row.text == source_text and row.title == original.title
        assert row.metrics["arxiv_version"] == 2 and row.metrics["like_count"] == 42
        assert row.source_id == "hf-papers" and row.priority
        assert set(session.scalars(select(Translation.id))) == cache_ids
        assert article_evidence(row)["evidence_type"] == "paper_abstract"
        assert digest_review_evidence(session, [{"id": row.id}], cfg)[0]["partial"]
    with store.begin() as session:
        assert ingest(session, [original], cfg, 1.0) == 0
        row = session.get(Article, first_id)
        assert row.metrics["like_count"] == 42 and row.source_id == "hf-papers" and row.priority
        assert session.get(ArticleReading, first_id).references[0]["url"] == "https://github.com/example/paper"
        assert set(session.scalars(select(Translation.id))) == cache_ids


def test_new_version_changes_evidence_and_queues_its_own_translation(store):
    cfg = config()
    with store.begin() as session:
        ingest(session, [paper(metrics={"arxiv_version": 1})], cfg)
        before = set(session.scalars(select(Translation.id)))
        updated = paper(metrics={"arxiv_version": 2}, text="arXiv preprint · Author abstract\n\nRevised assumptions.")
        assert ingest(session, [updated], cfg) == 0
        assert session.scalar(select(Article)).text == updated.text
        assert len(set(session.scalars(select(Translation.id))) - before) == 1


@pytest.mark.asyncio
async def test_collection_persists_results_and_refresh_cadence(store, monkeypatch):
    cfg = config(research=ResearchConfig(hf_enabled=True, arxiv_enabled=True))
    pipeline = Pipeline(store, cfg)
    hf = AsyncMock(return_value=ResearchResult([paper("hf-papers")], "partial", "一条无效记录已跳过。"))
    arxiv = AsyncMock(return_value=ResearchResult([], "healthy", "本轮无新增理论论文。"))
    monkeypatch.setattr("radar.pipeline.fetch_hf_papers", hf)
    monkeypatch.setattr("radar.pipeline.fetch_arxiv_theory", arxiv)
    monkeypatch.setattr("radar.pipeline.fetch_facebook", AsyncMock(return_value=[]))
    monkeypatch.setattr("radar.pipeline.XCollector.collect", AsyncMock(return_value=XCollectionResult()))
    assert await pipeline.collect() == 1
    assert await pipeline.collect() == 0
    assert hf.await_count == arxiv.await_count == 1
    with store.begin() as session:
        h = session.get(SourceState, "hf-papers")
        assert h.status == "partial" and h.last_success_at and h.item_count == 1
        assert session.get(SourceState, "arxiv-theory").status == "healthy"
        h.last_attempt_at = (datetime.now(UTC) - timedelta(hours=1, minutes=1)).isoformat()
    assert await pipeline.collect() == 0
    assert hf.await_count == 2 and arxiv.await_count == 1

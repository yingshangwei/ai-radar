"""Official API abstracts reuse ordinary caches without fetching paper pages."""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from radar.config import RadarConfig
from radar.db import database
from radar.models import Article, ArticleDocument, DocumentAnalysis, SummaryReview, Translation, WebDocument
from radar.pipeline import ingest
from radar.reading import (
    RESEARCH_ABSTRACT_PREFIX,
    ReadingService,
    analysis_key,
    cache_research_abstract,
    fingerprint,
    remember_references,
    sync_documents,
)
from radar.schemas import IncomingArticle
from radar.translation import TranslationService, cache_key, queue_article
from radar.web_reader import PageFetcher

PAPER_ID = "2609.01234"
PAPER_URL = "https://arxiv.org/abs/" + PAPER_ID
ABSTRACT = (RESEARCH_ABSTRACT_PREFIX + "The authors propose an AI model and report results on a synthetic "
            "benchmark. The reported improvement is a claim by the authors, not an independent replication.")


@pytest.fixture
def harness(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/papers.db")
    config = RadarConfig(provider={"kind": "command"}, translation={"enabled": True},
                         summary_review={"max_correction_rounds": 0}, reading={"enabled": True})
    yield sessions, config
    engine.dispose()


def paper(**kwargs):
    return IncomingArticle(**({
        "platform": "web", "source_id": "hf-papers", "external_id": "arxiv:" + PAPER_ID,
        "url": PAPER_URL, "title": "AI research paper", "text": ABSTRACT,
        "author": "Synthetic Authors", "published_at": datetime.now(UTC), "metrics": {"arxiv_version": 1},
    } | kwargs))


def add(sessions, config, *, refs=None, **kwargs):
    incoming = paper(**kwargs)
    with sessions.begin() as session:
        article = Article(id="paper", canonical_url=incoming.url,
                          **incoming.model_dump(exclude={"references", "published_at"}),
                          published_at=incoming.published_at.isoformat())
        session.add(article)
        session.flush()
        queue_article(session, article, config.translation)
        remember_references(session, article, refs or [])
        cached = cache_research_abstract(session, article, config)
        sync_documents(session, article, config)
        session.flush()
        return article.id, cached.id if cached else None


def snapshot(row):
    return deepcopy({column.name: getattr(row, column.name) for column in row.__table__.columns})


@pytest.mark.parametrize("source_id", ["hf-papers", "arxiv-theory"])
def test_normal_ingest_caches_api_abstract_before_reading_binding(harness, source_id):
    sessions, config = harness
    with sessions.begin() as session:
        assert ingest(session, [paper(source_id=source_id)], config) == 1
        article = session.scalar(select(Article))
        root = session.get(WebDocument, fingerprint(PAPER_URL))
        assert root is not None and root.status == "fetched"
        assert root.title == article.title and root.text == article.text == ABSTRACT
        assert root.partial is True and root.content_type == "text/plain"
        assert root.content_hash == fingerprint(article.title, article.text, True)
        assert root.fetched_at and root.analysis_id == ""
        assert session.get(ArticleDocument, (article.id, root.id)).relation == "source"
        assert session.scalar(select(DocumentAnalysis)) is None
        assert session.scalar(select(SummaryReview)) is None


def test_abstract_reuses_same_translation_and_unchanged_analysis_cache(harness):
    sessions, config = harness
    article_id, doc_id = add(sessions, config)
    with sessions.begin() as session:
        article, doc = session.get(Article, article_id), session.get(WebDocument, doc_id)
        key = cache_key(article.title, article.text, config.translation)
        assert key == cache_key(doc.title, doc.text, config.translation)
        translation = session.get(Translation, key)
        translation.status, translation.title_zh, translation.text_zh = "ready", "合成论文", "这是作者摘要。"
        doc.analysis_id = analysis_key(session, doc, config)
        session.add(DocumentAnalysis(id=doc.analysis_id, status="ready", title_zh="合成论文摘要"))
        session.flush()
        before, analysis_id = snapshot(translation), doc.analysis_id
        cache_research_abstract(session, article, config)
        assert doc.analysis_id == analysis_id
        assert snapshot(translation) == before
        assert len(session.scalars(select(Translation)).all()) == 1


def test_changed_api_abstract_invalidates_binding_without_rewriting_old_review(harness):
    sessions, config = harness
    article_id, doc_id = add(sessions, config)
    with sessions.begin() as session:
        article, doc = session.get(Article, article_id), session.get(WebDocument, doc_id)
        old_hash = doc.content_hash
        doc.analysis_id = analysis_key(session, doc, config)
        old = DocumentAnalysis(id=doc.analysis_id, status="ready", title_zh="原有解读")
        session.add(old)
        session.flush()
        before = snapshot(old)
        article.text += " The updated abstract states an additional limitation."
        article.metrics = {"arxiv_version": 2}
        cache_research_abstract(session, article, config)
        assert doc.content_hash != old_hash and doc.analysis_id == ""
        assert doc.text == article.text and doc.partial is True
        assert snapshot(old) == before


@pytest.mark.parametrize("partial,content_type", [(False, "text/html"), (True, "application/pdf"),
                                                 (True, "text/plain")])
def test_existing_paper_body_is_never_downgraded_to_abstract(harness, partial, content_type):
    sessions, config = harness
    article_id, doc_id = add(sessions, config)
    with sessions.begin() as session:
        doc = session.get(WebDocument, doc_id)
        doc.title, doc.text = "Previously fetched paper", "Previously obtained detailed paper body. " * 10
        doc.partial, doc.content_type, doc.analysis_id = partial, content_type, "existing-review"
        doc.content_hash = fingerprint(doc.title, doc.text, partial)
        doc.links = [{"url": "https://example.org/project", "label": "Original link"}]
        before = snapshot(doc)
        cache_research_abstract(session, session.get(Article, article_id), config)
        assert snapshot(doc) == before


@pytest.mark.parametrize("change", [
    {"source_id": "import"}, {"platform": "rss"}, {"external_id": "not-a-paper"},
    {"external_id": "arxiv:2609.01234v1"}, {"url": "https://arxiv.org/pdf/2609.01234"},
    {"url": PAPER_URL + "?query=1"}, {"url": "https://arxiv.org.evil.example/abs/2609.01234"},
    {"text": "This is not the official abstract contract."}, {"text": RESEARCH_ABSTRACT_PREFIX},
])
def test_cache_helper_requires_narrow_official_abstract_contract(harness, change):
    sessions, config = harness
    with sessions.begin() as session:
        incoming = paper(**change)
        article = Article(id="invalid", canonical_url=incoming.url,
                          **incoming.model_dump(exclude={"references", "published_at"}),
                          published_at=incoming.published_at.isoformat())
        session.add(article)
        session.flush()
        assert cache_research_abstract(session, article, config) is None
        assert session.scalar(select(WebDocument)) is None


def test_research_links_only_use_explicit_non_pdf_project_code_or_discussion(harness):
    sessions, config = harness
    explicit = [
        {"url": "https://arxiv.org/pdf/2609.01234", "label": "PDF"},
        {"url": "https://arxiv.org/html/2609.01234v1", "label": "Other format"},
        {"url": "https://example.org/paper.PDF?download=1", "label": "PDF mirror"},
        {"url": "https://arxiv.org/abs/2609.99999", "label": "Cited paper"},
        {"url": "https://github.com/synthetic/project", "label": "Code"},
        {"url": "https://example.org/project", "label": "Project"},
        {"url": "https://huggingface.co/papers/2609.01234", "label": "Discussion"},
    ]
    article_id, doc_id = add(sessions, config, refs=explicit,
                              text=ABSTRACT + " ChatGPT https://example.org/implicit")
    with sessions.begin() as session:
        doc = session.get(WebDocument, doc_id)
        doc.links = [{"url": "https://example.org/root-discovered", "label": "Must not expand"}]
        child = session.get(WebDocument, fingerprint("https://example.org/project"))
        child.links = [{"url": "https://example.org/deep-child", "label": "Must not recurse"}]
        sync_documents(session, session.get(Article, article_id), config)
        urls = set(session.scalars(select(WebDocument.url).join(ArticleDocument).where(
            ArticleDocument.article_id == article_id)))
        assert urls == {PAPER_URL, "https://github.com/synthetic/project", "https://example.org/project",
                        "https://huggingface.co/papers/2609.01234"}
        assert session.get(Article, article_id).url == PAPER_URL


@pytest.mark.parametrize("force", [False, True])
async def test_reading_uses_cached_partial_evidence_never_fetches_root_even_when_expired(harness, monkeypatch, force):
    sessions, config = harness
    _, doc_id = add(sessions, config)
    with sessions.begin() as session:
        session.get(WebDocument, doc_id).retry_at = "2000-01-01T00:00:00+00:00"
    calls, seen = [], []

    class Model:
        async def complete(self, prompt, schema):
            calls.append(schema.__name__)
            if schema.__name__ == "ReadingOutput":
                documents = json.loads(prompt.split("\nUNTRUSTED_DOCUMENTS:\n")[1])
                seen.extend(documents)
                return json.dumps({"documents": [{"source_id": doc["id"], "title_zh": "作者摘要中的模型研究",
                    "summary_zh": "仅依据作者摘要，作者声称模型取得实验改进，尚未阅读完整论文。",
                    "key_points_zh": ["实验结果属于作者陈述。"], "why_it_matters_zh": "可供后续研究参考。"}
                    for doc in documents]})
            payload = json.loads(prompt.split("\nUNTRUSTED_REVIEW_INPUT:\n")[1])
            assert all(source["partial"] for source in payload["frozen_sources"])
            return json.dumps({"audits": [{"unit_id": unit["unit_id"], "approved": True, "issues": []}
                                           for unit in payload["units"]]})

    async def no_network(*args, **kwargs):
        pytest.fail("Research API abstract must not trigger webpage fetching or translation here")

    model = Model()
    monkeypatch.setattr("radar.reading.make_provider", lambda _: model)
    monkeypatch.setattr("radar.summary_review.make_provider", lambda _: model)
    monkeypatch.setattr(PageFetcher, "bytes", no_network)
    translator = TranslationService(sessions, config.translation)
    monkeypatch.setattr(translator, "translate_one", no_network)
    service = ReadingService(sessions, config, translator)
    await service.fetch_one(doc_id, PageFetcher())
    first = await service.pending(force=force, translate=False, limit=1)
    second = await service.pending(force=force, translate=False, limit=1)
    assert first["fetched"] == second["fetched"] == 0
    assert first["summarized"] == 1 and second["summarized"] == 0
    assert calls == ["ReadingOutput", "SummaryAuditOutput"]
    assert seen[0]["text"] == ABSTRACT and seen[0]["partial"] is True
    assert service.has_pending() is False


def test_hf_paper_page_is_reference_metadata_not_paper_evidence(harness):
    sessions, config = harness
    refs = [
        {"url": "https://huggingface.co/papers/2609.01234", "label": "社区讨论"},
        {"url": "https://huggingface.co/papers", "label": "论文索引"},
        {"url": "https://github.com/example/project", "label": "代码"},
        {"url": "https://huggingface.co/spaces/example/project", "label": "应用"},
    ]
    article_id, root_id = add(sessions, config, refs=refs)
    with sessions() as session:
        from radar.models import ArticleReading
        assert session.get(ArticleReading, article_id).references == refs
        bound = session.scalars(select(WebDocument).join(ArticleDocument).where(
            ArticleDocument.article_id == article_id)).all()
        assert {doc.url for doc in bound} == {
            PAPER_URL, "https://github.com/example/project", "https://huggingface.co/spaces/example/project",
        }
        assert all(not doc.url.startswith("https://huggingface.co/papers") for doc in bound)

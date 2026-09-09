import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.api import create_app
from radar.article_presentation import ArticlePresentationService, presentation_views, social_views
from radar.config import ProviderConfig, RadarConfig, ReadingConfig, Settings, SummaryReviewConfig
from radar.db import database
from radar.models import (
    Article,
    ArticleDocument,
    ArticleReading,
    DocumentAnalysis,
    SummaryReview,
    WebDocument,
)
from radar.pipeline import Pipeline, ingest
from radar.reading import analysis_key, fingerprint, sync_documents
from radar.sources import x_page_items

TITLE = "模型新增工具调用能力"


class Model:
    def __init__(self):
        self.calls = []
        self.reject = False

    async def complete(self, prompt, schema):
        self.calls.append(schema.__name__)
        if schema.__name__ == "ReadingOutput":
            docs = json.loads(prompt.split("\nUNTRUSTED_DOCUMENTS:\n")[1])
            return json.dumps({"documents": [{"source_id": doc["id"], "title_zh": TITLE,
                "summary_zh": "模型新增工具调用能力。", "key_points_zh": ["模型可以调用工具。"],
                "why_it_matters_zh": "可以辅助用户完成任务。"} for doc in docs]})
        assert schema.__name__ == "SummaryAuditOutput"
        payload = json.loads(prompt.split("\nUNTRUSTED_REVIEW_INPUT:\n")[1])
        return json.dumps({"audits": [{"unit_id": unit["unit_id"], "approved": not self.reject,
            "issues": [{"field": "title_zh", "reason": "标题没有证据支持", "source_ids": unit["source_ids"]}]
            if self.reject else []} for unit in payload["units"]]})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    config = RadarConfig(provider=ProviderConfig(kind="command"),
                         summary_review=SummaryReviewConfig(max_correction_rounds=0, max_format_retries=0),
                         reading=ReadingConfig(enabled=True, mention_catalog={}))
    engine, sessions = database(f"sqlite:///{tmp_path}/cards.db")
    model = Model()
    monkeypatch.setattr("radar.summary_review.make_provider", lambda _: model)
    yield sessions, config, model
    engine.dispose()


def add(session, uid="a", *, platform="x", minutes=0):
    row = Article(id=uid, platform=platform, source_id="fixture", external_id=uid,
                  url=f"https://example.org/{uid}", canonical_url=f"https://example.org/{uid}",
                  title="AI tools", text="The AI model supports tool use.", author="Author", handle="author",
                  published_at=(datetime.now(UTC) - timedelta(minutes=minutes)).isoformat())
    session.add(row)
    session.flush()
    return row


def original_rows(sessions):
    with sessions() as session:
        return {a.id: deepcopy({c.name: getattr(a, c.name) for c in a.__table__.columns})
                for a in session.scalars(select(Article))}


@pytest.mark.asyncio
async def test_saved_heading_reused_and_current_original_required(harness):
    sessions, config, model = harness
    with sessions.begin() as session:
        add(session)
    originals = original_rows(sessions)
    service = ArticlePresentationService(sessions, config, lambda _: model)
    assert await service.pending() == {"processed": 1, "ready": 1}
    assert model.calls == ["ReadingOutput", "SummaryAuditOutput"]
    with sessions() as session:
        article = session.get(Article, "a")
        assert presentation_views(session, [article], config)["a"] == {"status": "ready", "title_zh": TITLE}
        saved = session.scalar(select(SummaryReview))
        assert saved.scope == "article:a" and saved.status == "ready"
    assert original_rows(sessions) == originals
    assert await ArticlePresentationService(sessions, config, lambda _: model).pending() == {"processed": 0, "ready": 0}
    assert len(model.calls) == 2
    with sessions.begin() as session:
        session.get(Article, "a").text = "The AI model supports a different task."
    with sessions() as session:
        assert presentation_views(session, [session.get(Article, "a")], config)["a"] == {
            "status": "pending", "title_zh": None}
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_technical_document_never_falls_back_to_an_old_separate_card_title(harness):
    sessions, config, model = harness
    with sessions.begin() as session:
        article = add(session, platform="web")
        article.text = r"The AI model uses an optimization rate $n^{-2}$."
    service = ArticlePresentationService(sessions, config, lambda _: model)
    assert (await service.pending())["ready"] == 1  # An earlier independent card cache exists.
    before_calls = list(model.calls)
    with sessions.begin() as session:
        article = session.get(Article, "a")
        session.add(WebDocument(id="doc", url=article.url, title=article.title, text=article.text,
                                content_hash=fingerprint(article.title, article.text, False)))
        session.flush()
        session.add(ArticleDocument(article_id="a", document_id="doc", relation="source"))
    with sessions() as session:
        assert presentation_views(session, [session.get(Article, "a")], config)["a"] == {
            "status": "pending", "title_zh": None}
    assert await service.pending() == {"processed": 0, "ready": 0}
    assert model.calls == before_calls
    with sessions.begin() as session:
        doc = session.get(WebDocument, "doc")
        doc.analysis_id = analysis_key(session, doc, config)
        session.add(DocumentAnalysis(id=doc.analysis_id, status="ready", title_zh="统一语境的论文标题"))
    with sessions() as session:
        assert presentation_views(session, [session.get(Article, "a")], config)["a"]["title_zh"] == "统一语境的论文标题"


@pytest.mark.asyncio
async def test_no_arbitrary_link_title_and_reuses_exact_current_source_page(harness):
    sessions, config, model = harness
    with sessions.begin() as session:
        article = add(session, platform="web")
        doc = WebDocument(id="doc", url=article.url, title=article.title, text=article.text, status="fetched",
                          content_hash=fingerprint(article.title, article.text, False))
        session.add(doc)
        session.flush()
        doc.analysis_id = analysis_key(session, doc, config)
        session.add(DocumentAnalysis(id=doc.analysis_id, status="ready", title_zh=TITLE))
        session.add(ArticleDocument(article_id="a", document_id="doc", relation="source"))
    service = ArticlePresentationService(sessions, config, lambda _: model)
    assert await service.pending() == {"processed": 0, "ready": 0}
    assert not model.calls
    with sessions.begin() as session:
        article = session.get(Article, "a")
        assert presentation_views(session, [article], config)["a"]["title_zh"] == TITLE
        binding = session.get(ArticleDocument, ("a", "doc"))
        binding.relation = "link"
    with sessions() as session:
        assert presentation_views(session, [session.get(Article, "a")], config)["a"]["title_zh"] is None
    with sessions.begin() as session:
        session.get(ArticleDocument, ("a", "doc")).relation = "source"
        session.get(Article, "a").text = "New original AI text."
    with sessions() as session:
        assert presentation_views(session, [session.get(Article, "a")], config)["a"]["title_zh"] is None


@pytest.mark.asyncio
async def test_pending_is_bounded_and_terminal_rejection_cannot_starve_following_articles(harness):
    sessions, config, model = harness
    with sessions.begin() as session:
        for index in range(5):
            add(session, str(index), minutes=index)
    model.reject = True
    service = ArticlePresentationService(sessions, config, lambda _: model)
    assert await service.pending() == {"processed": 3, "ready": 0}
    assert len(model.calls) == 6
    model.reject = False
    assert await service.pending() == {"processed": 2, "ready": 2}
    assert len(model.calls) == 10
    with sessions() as session:
        rows = list(session.scalars(select(Article).order_by(Article.id)))
        views = presentation_views(session, rows, config)
        assert views["0"] == {"status": "review_required", "title_zh": None}
        assert views["3"] == {"status": "ready", "title_zh": TITLE}
    assert await service.pending() == {"processed": 0, "ready": 0}


@pytest.mark.asyncio
async def test_article_gets_are_read_only_and_never_create_heading_work(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[provider]\nkind="command"\n[reading]\nenabled=false\n')
    settings = Settings(config_path=str(path), database_url=f"sqlite:///{tmp_path}/api.db",
                        reader_token="reader", admin_token="admin", scheduler_enabled=False, _env_file=None)
    app = create_app(settings)
    with app.state.sessions.begin() as session:
        add(session)

    async def forbidden(*args, **kwargs):
        raise AssertionError("GET must never schedule or invoke models")

    monkeypatch.setattr(ArticlePresentationService, "pending", forbidden)
    monkeypatch.setattr("radar.summary_review.SummaryReviewService.analyze_documents", forbidden)
    with TestClient(app) as client:
        for url in ["/v1/articles", "/v1/articles/a", "/v1/articles"]:
            response = client.get(url, headers={"Authorization": "Bearer reader"})
            assert response.status_code == 200
            item = response.json()["items"][0] if "items" in response.json() else response.json()
            assert item["presentation"] == {"status": "pending", "title_zh": None}
            assert item["social"] == {"reply_to": None}
    with app.state.sessions() as session:
        assert not session.scalars(select(SummaryReview)).all()


def test_reply_identity_is_api_metadata_and_never_adds_parent_body_or_fetch(harness):
    sessions, config, _ = harness
    now = datetime.now(UTC).isoformat()
    items = x_page_items({"data": [{"id": "100", "author_id": "1", "created_at": now,
        "text": "AI tools can help research.", "public_metrics": {"like_count": 50},
        "referenced_tweets": [{"type": "replied_to", "id": "200"}]}],
        "includes": {"tweets": [{"id": "200", "author_id": "2", "created_at": now,
                                  "text": "Parent text should not be collected."}],
                     "users": [{"id": "1", "name": "Writer", "username": "writer"},
                               {"id": "2", "name": "Researcher", "username": "researcher"}]}})
    assert items[0].text == "AI tools can help research."
    with sessions.begin() as session:
        assert ingest(session, items, config) == 1
        article = session.scalar(select(Article))
        uid = article.id
        sync_documents(session, article, config)
        assert not session.scalars(select(WebDocument)).all()
        refs = session.get(ArticleReading, uid).references
        assert len(refs) == 1 and refs[0]["kind"] == "reply"
        assert "Parent text" not in json.dumps(refs)
        assert social_views(session, [uid])[uid]["reply_to"] == {
            "url": "https://x.com/i/status/200", "author": "Researcher", "handle": "researcher",
            "published_at": now}


def test_leading_handle_does_not_invent_reply_and_missing_parent_keeps_only_known_url(harness):
    sessions, config, _ = harness
    now = datetime.now(UTC).isoformat()
    items = x_page_items({"data": [{"id": "100", "author_id": "1", "created_at": now,
        "text": "@someone AI tools are useful.", "public_metrics": {"like_count": 50}},
        {"id": "101", "author_id": "1", "created_at": now, "text": "AI work",
         "public_metrics": {"like_count": 50}, "referenced_tweets": [{"type": "replied_to", "id": "201"}]}]})
    assert not items[0].references
    assert items[1].references[0].author == "" and items[1].references[0].published_at is None
    with sessions.begin() as session:
        ingest(session, items, config)
        rows = list(session.scalars(select(Article).order_by(Article.external_id)))
        views = social_views(session, [row.id for row in rows])
        assert views[rows[0].id]["reply_to"] is None
        assert views[rows[1].id]["reply_to"]["url"] == "https://x.com/i/status/201"


@pytest.mark.asyncio
async def test_normal_job_uses_background_service_but_translation_only_does_not(harness, monkeypatch):
    sessions, config, _ = harness
    pipeline = Pipeline(sessions, config)
    calls = []

    async def pending():
        calls.append("headings")
        return {"processed": 0, "ready": 0}

    async def reading(**kwargs):
        return {"fetched": 0, "summarized": 0}

    monkeypatch.setattr(pipeline.presentations, "pending", pending)
    monkeypatch.setattr(pipeline.reading, "pending", reading)
    await pipeline.run("read")
    await pipeline.run("translate")
    assert calls == ["headings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("translation_status", ["ready", "review_required"])
async def test_only_saved_ready_chinese_is_generation_material_not_a_new_review_key(harness, translation_status):
    from radar.models import Translation
    from radar.translation import ensure_translation

    sessions, config, model = harness
    config = config.model_copy(update={"translation": config.translation.model_copy(update={"enabled": True})})
    with sessions.begin() as session:
        article = add(session)
        cache = ensure_translation(session, article.title, article.text, config.translation)
        cache.status = translation_status
        cache.title_zh, cache.text_zh = "已保存中文标题", "已保存中文正文，仅供生成复用。"
    service = ArticlePresentationService(sessions, config, lambda _: model)
    await service.pending()
    with sessions() as session:
        row = session.scalar(select(SummaryReview))
        material = row.generator_sources[0]
        assert (material.get("text_zh") == "已保存中文正文，仅供生成复用。") is (translation_status == "ready")
        assert "text_zh" not in row.evidence[0]  # Independent audit stays grounded in original text.
    with sessions.begin() as session:
        session.get(Article, "a").metrics = {"like_count": 999}
        cache = session.scalar(select(Translation))
        cache.status, cache.text_zh = "ready", "此后保存的中文正文。"
    assert await service.pending() == {"processed": 0, "ready": 0}
    assert model.calls == ["ReadingOutput", "SummaryAuditOutput"]

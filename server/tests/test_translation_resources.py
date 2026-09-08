import copy
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from openai import APIStatusError
from sqlalchemy import select

from radar.config import RadarConfig, ReadingConfig, TranslationConfig
from radar.db import database
from radar.models import Article, ArticleDocument, ArticleTranslation, Job, Translation, WebDocument
from radar.pipeline import Pipeline, ingest
from radar.schemas import IncomingArticle
from radar.translation import (
    RECHECK_POLICY,
    AuditedPart,
    TranslatedPart,
    TranslationService,
    cache_key,
    candidate_fingerprint,
    ensure_translation,
    translation_status,
)

MAIN = "AI agents are useful."
WEB = "AI models can help researchers."
OTHER = "AI tools support science."
CHINESE = {
    MAIN: "智能体很有用。",
    WEB: "人工智能模型可以帮助研究人员。",
    OTHER: "人工智能工具支持科学研究。",
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/resources.db")
    yield sessions
    engine.dispose()


def add_article(sessions, config, uid="main", *, text=MAIN, ready=True, age=0):
    with sessions.begin() as session:
        ingest(session, [IncomingArticle(
            platform="x", external_id=uid, title=text, text=text, author="OpenAI", handle="OpenAI",
            url=f"https://x.com/OpenAI/status/{uid}", published_at=datetime.now(UTC)-timedelta(hours=age),
        )], RadarConfig(translation=config))
        article = session.scalar(select(Article).where(Article.external_id == uid))
        key = session.get(ArticleTranslation, article.id).translation_id
        if ready:
            row = session.get(Translation, key)
            row.status, row.title_zh, row.text_zh = "ready", CHINESE[text], CHINESE[text]
        return article.id, key


def add_document(sessions, uid, article_ids=(), *, text=WEB):
    with sessions.begin() as session:
        session.add(WebDocument(
            id=uid, url=f"https://example.org/{uid}", title=text, text=text, status="fetched",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        ))
        session.flush()
        for article_id in article_ids:
            session.add(ArticleDocument(article_id=article_id, document_id=uid, relation="link"))


def row_snapshot(sessions, key):
    with sessions() as session:
        row = session.get(Translation, key)
        return copy.deepcopy({column.name: getattr(row, column.name) for column in row.__table__.columns})


def model_responses(service):
    calls = []

    async def request(parts, *, review):
        calls.extend(("review" if review else "draft", part["source"]) for part in parts)
        return {part["id"]: TranslatedPart(id=part["id"], zh=CHINESE[part["source"]], approved=True)
                for part in parts}

    async def audit(parts):
        calls.extend(("audit", part["source"]) for part in parts)
        return {part["id"]: AuditedPart(id=part["id"], approved=True) for part in parts}

    service.request, service.audit = request, audit
    return calls


@pytest.mark.asyncio
async def test_pending_translates_bound_saved_body_and_reuses_ready_main_article(store):
    config = TranslationConfig(enabled=True)
    article, main_key = add_article(store, config)
    add_document(store, "web", [article])
    main_before = row_snapshot(store, main_key)
    service = TranslationService(store, config)
    calls = model_responses(service)

    result = await service.pending()

    assert calls == [(stage, WEB) for stage in ["draft", "review", "audit"]]
    assert result["counts"] == {"ready": 1} and result["resource_counts"] == {"ready": 1}
    assert row_snapshot(store, main_key) == main_before
    with store() as session:
        translated = session.get(Translation, cache_key(WEB, WEB, config))
        assert translated.status == "ready" and translated.text_zh == CHINESE[WEB]


@pytest.mark.asyncio
async def test_force_resumes_exhausted_web_draft_without_repeating_approved_parts(store):
    config = TranslationConfig(enabled=True)
    article, _ = add_article(store, config)
    text = WEB + "\n\n" + OTHER
    add_document(store, "web", [article], text=text)
    audited = {
        "id": "body-0", "source": WEB, "zh": CHINESE[WEB], "draft": CHINESE[WEB], "ok": True,
        "audit": {"policy": RECHECK_POLICY, "approved": True, "issues": [],
                  "fingerprint": candidate_fingerprint(WEB, CHINESE[WEB])},
    }
    with store.begin() as session:
        row = ensure_translation(session, text, text, config)
        key = row.id
        row.parts = [copy.deepcopy(audited), {
            "id": "body-1", "source": OTHER, "draft": CHINESE[OTHER], "zh": CHINESE[OTHER],
            "ok": False, "correction_required": False,
        }]
    failed = TranslationService(store, config)
    failed_calls = []

    async def ended_timeout(parts):
        failed_calls.extend(part["source"] for part in parts)
        raise TimeoutError("synthetic completed audit timeout")

    failed.audit = ended_timeout
    await failed.translate_one(key)
    assert failed_calls == [OTHER]
    with store.begin() as session:
        row = session.get(Translation, key)
        assert row.status == "error" and row.parts[0] == audited
        # Exhaust only the older document-level limit. The actual completed
        # failure receipt, not the status string, authorizes bounded recovery.
        row.attempts = config.max_attempts
        row.retry_at = (datetime.now(UTC)+timedelta(hours=1)).isoformat()
    service = TranslationService(store, config)
    calls = model_responses(service)
    await service.pending()
    assert not calls

    result = await service.pending(force=True)

    assert calls == [("audit", OTHER)]
    assert result["resource_counts"] == {"ready": 1}
    with store() as session:
        row = session.get(Translation, key)
        assert row.parts[0] == audited
        assert row.original_text == text and row.attempts == config.max_attempts+1
        assert row.text_zh == CHINESE[WEB]+"\n\n"+CHINESE[OTHER]


@pytest.mark.asyncio
async def test_shared_message_and_web_cache_processed_once_but_documents_counted_individually(store):
    config = TranslationConfig(enabled=True)
    first, shared = add_article(store, config, "a", ready=False)
    second, twin = add_article(store, config, "b", ready=False)
    assert shared == twin
    add_document(store, "web-one", [first, second], text=MAIN)
    add_document(store, "web-two", [first], text=MAIN)
    service = TranslationService(store, config)
    calls = model_responses(service)

    result = await service.pending()

    assert calls == [(stage, MAIN) for stage in ["draft", "review", "audit"]]
    assert result["counts"] == {"ready": 2} and result["resource_counts"] == {"ready": 2}


@pytest.mark.asyncio
async def test_changed_body_uses_current_cache_and_excludes_orphan_and_empty_documents(store):
    config = TranslationConfig(enabled=True)
    article, _ = add_article(store, config)
    add_document(store, "changed", [article])
    add_document(store, "orphan", text=WEB)
    add_document(store, "empty", [article], text="")
    add_document(store, "whitespace", [article], text=" \n\t ")
    with store.begin() as session:
        old = ensure_translation(session, WEB, WEB, config)
        old.status = "error"
        old_key = old.id
        document = session.get(WebDocument, "changed")
        document.title = document.text = OTHER
        document.content_hash = hashlib.sha256(OTHER.encode()).hexdigest()
    before = row_snapshot(store, old_key)
    service = TranslationService(store, config)
    calls = model_responses(service)

    result = await service.pending(force=True)

    assert calls == [(stage, OTHER) for stage in ["draft", "review", "audit"]]
    assert result["resource_counts"] == {"ready": 1}
    assert row_snapshot(store, old_key) == before
    with store() as session:
        assert session.get(Translation, cache_key(OTHER, OTHER, config)).status == "ready"
        assert session.get(Translation, cache_key("", "", config)) is None
        assert session.get(Translation, cache_key(" \n\t ", " \n\t ", config)) is None


@pytest.mark.asyncio
async def test_main_messages_have_priority_with_shared_document_queue_limit(store):
    config = TranslationConfig(enabled=True, max_documents=1)
    article, main = add_article(store, config, ready=False)
    add_document(store, "web", [article])
    service = TranslationService(store, config)
    calls = model_responses(service)

    result = await service.pending()

    assert calls == [(stage, MAIN) for stage in ["draft", "review", "audit"]]
    assert result["counts"] == {"ready": 1} and result["resource_counts"] == {"pending": 1}
    with store() as session:
        assert session.get(Translation, main).status == "ready"
        assert session.get(Translation, cache_key(WEB, WEB, config)).attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["exhausted", "backoff", "lease"])
async def test_ineligible_main_message_does_not_starve_eligible_web_document(store, blocked):
    config = TranslationConfig(enabled=True, max_documents=1)
    article, main = add_article(store, config, ready=False)
    add_document(store, "web", [article])
    with store.begin() as session:
        row = session.get(Translation, main)
        if blocked == "exhausted":
            row.attempts = config.max_attempts
        elif blocked == "backoff":
            row.retry_at = (datetime.now(UTC)+timedelta(hours=1)).isoformat()
        else:
            row.lease_until = (datetime.now(UTC)+timedelta(hours=1)).isoformat()
            row.owner = "another-worker"
    before = row_snapshot(store, main)
    service = TranslationService(store, config)
    calls = model_responses(service)

    result = await service.pending()

    assert calls == [(stage, WEB) for stage in ["draft", "review", "audit"]]
    assert result["resource_counts"] == {"ready": 1} and row_snapshot(store, main) == before


def test_translation_status_is_read_only_and_reports_current_resource_cache(store, monkeypatch):
    config = TranslationConfig(enabled=True)
    article, _ = add_article(store, config)
    add_document(store, "one", [article])
    add_document(store, "two", [article])
    add_document(store, "empty", [article], text="")
    add_document(store, "orphan", text=OTHER)
    with store.begin() as session:
        stale = ensure_translation(session, OTHER, OTHER, config)
        stale.status = "ready"
    before = {}
    with store() as session:
        before = {row.id: copy.deepcopy(row.parts) for row in session.scalars(select(Translation))}
    monkeypatch.setattr(TranslationService, "request", lambda *args, **kwargs: pytest.fail("Status called model"))
    monkeypatch.setattr(TranslationService, "audit", lambda *args, **kwargs: pytest.fail("Status called auditor"))
    with store() as session:
        first = translation_status(session, config)
        second = translation_status(session, config)
        assert first == second
        assert first["counts"] == {"ready": 1} and first["resource_counts"] == {"pending": 2}
        assert {row.id: row.parts for row in session.scalars(select(Translation))} == before
        assert session.get(Translation, cache_key(WEB, WEB, config)) is None


@pytest.mark.asyncio
async def test_resource_balance_error_stops_batch_without_exhausting_other_pages(store):
    config = TranslationConfig(enabled=True, concurrency=1)
    article, _ = add_article(store, config)
    add_document(store, "a-first", [article])
    add_document(store, "b-second", [article], text=OTHER)
    service = TranslationService(store, config)
    calls = []

    async def depleted(parts, *, review):
        calls.append(parts[0]["source"])
        raise APIStatusError("secret fixture", response=httpx.Response(
            402, request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        ), body={})

    service.request = depleted
    result = await service.pending(force=True)
    assert calls == [WEB]
    assert result["resource_counts"] == {"insufficient_balance": 1, "pending": 1}
    assert result["alert"]["code"] == "insufficient_balance"
    with store() as session:
        assert session.get(Translation, cache_key(WEB, WEB, config)).attempts == 0
        assert session.get(Translation, cache_key(OTHER, OTHER, config)).attempts == 0


@pytest.mark.asyncio
async def test_translate_job_only_translates_saved_resources_and_reports_remaining(store, monkeypatch):
    translation = TranslationConfig(enabled=True, max_documents=1)
    article, _ = add_article(store, translation)
    add_document(store, "a-first", [article])
    add_document(store, "b-second", [article], text=OTHER)
    config = RadarConfig(translation=translation, reading=ReadingConfig(enabled=True))
    pipeline = Pipeline(store, config)
    calls = model_responses(pipeline.translations)

    async def forbidden(*args, **kwargs):
        pytest.fail("Translate job fetched a page or generated a summary")

    monkeypatch.setattr(pipeline, "collect", forbidden)
    monkeypatch.setattr(pipeline, "digest", forbidden)
    monkeypatch.setattr(pipeline.reading, "pending", forbidden)
    uid = await pipeline.run(kind="translate", force=True)

    assert calls == [(stage, WEB) for stage in ["draft", "review", "audit"]]
    with store() as session:
        job = session.get(Job, uid)
        assert job.status == "completed"
        assert "网页正文中文版本 1 份" in job.message
        assert "1 份仍在等待翻译或校对" in job.message
        assert session.get(Translation, cache_key(OTHER, OTHER, translation)).status == "pending"

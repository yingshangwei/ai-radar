"""Real SQLite reading queues with synthetic model transport only."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from radar.config import RadarConfig
from radar.db import database
from radar.models import Article, ArticleDocument, DocumentAnalysis, SummaryReview, Translation, WebDocument
from radar.providers import StructuredProvider
from radar.reading import ReadingService, analysis_key, fingerprint
from radar.summary_evidence import document_review_evidence
from radar.summary_review import SummaryReviewPending
from radar.translation import AuditedPart, TranslatedPart, TranslationService, ensure_translation
from radar.web_reader import PageFetcher, PageUnavailable

CHINESE = "这款合成人工智能模型支持研究。"


class ReviewModel(StructuredProvider):
    def __init__(self):
        self.calls = []
        self.sources = []

    async def complete(self, prompt, schema):
        self.calls.append(schema.__name__)
        if schema.__name__ == "ReadingOutput":
            sources = json.loads(prompt.split("\nUNTRUSTED_DOCUMENTS:\n", 1)[1])
            self.sources.extend(deepcopy(sources))
            return json.dumps({"documents": [{"source_id": source["id"], **summary_fields()}
                                              for source in sources]})
        assert schema.__name__ == "SummaryAuditOutput"
        units = json.loads(prompt.split("\nUNTRUSTED_REVIEW_INPUT:\n", 1)[1])["units"]
        return json.dumps({"audits": [{"unit_id": unit["unit_id"], "approved": True, "issues": []}
                                       for unit in units]})


def summary_fields():
    return {"title_zh": "合成人工智能研究", "summary_zh": CHINESE,
            "key_points_zh": ["这是一份合成测试材料。"], "why_it_matters_zh": "为研究提供支持。"}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-only")
    engine, sessions = database(f"sqlite:///{tmp_path}/backlog.db")
    config = RadarConfig(translation={"enabled": True, "concurrency": 1},
                         reading={"enabled": True, "max_documents": 1, "mention_catalog": {}})
    model = ReviewModel()
    monkeypatch.setattr("radar.reading.make_provider", lambda _: model)
    monkeypatch.setattr("radar.summary_review.make_provider", lambda _: model)

    async def no_fetch(*args, **kwargs):
        pytest.fail("Already saved synthetic pages must not be fetched")

    monkeypatch.setattr(PageFetcher, "bytes", no_fetch)
    yield sessions, config, model
    engine.dispose()


def add_document(sessions, config, name, *, hours=0, analyzed=False):
    url = f"https://example.org/{name}"
    text = f"This synthetic AI model supports {name} research."
    with sessions.begin() as session:
        session.add(Article(id=name, platform="rss", source_id="fixture", external_id=name,
                            url=url, canonical_url=url, author="Synthetic researcher", title=text, text=text,
                            published_at=(datetime.now(UTC) - timedelta(hours=hours)).isoformat()))
        document = WebDocument(id=fingerprint(url), url=url, final_url=url, title=text, text=text,
                               content_hash=fingerprint(text, text, False), status="fetched",
                               fetched_at=datetime.now(UTC).isoformat(), retry_at="2999-01-01")
        session.add(document)
        session.flush()
        session.add(ArticleDocument(article_id=name, document_id=document.id, relation="source"))
        key = ensure_translation(session, text, text, config.translation).id
        if analyzed:
            document.analysis_id = analysis_key(session, document, config)
            session.add(DocumentAnalysis(id=document.analysis_id, status="ready", **summary_fields()))
    return document.id, key


def translation_snapshot(sessions, key):
    with sessions() as session:
        row = session.get(Translation, key)
        return deepcopy({column.name: getattr(row, column.name) for column in row.__table__.columns})


async def populate_translation(service, key, *, approved=True, crash_after_receipts=False):
    async def request(parts, *, review):
        return {part["id"]: TranslatedPart(id=part["id"], zh=CHINESE, approved=True) for part in parts}

    async def audit(parts):
        return {part["id"]: AuditedPart(id=part["id"], approved=approved,
                issues=[] if approved else ["候选仍遗漏来源限定。"])
                for part in parts}

    service.request, service.audit = request, audit
    if crash_after_receipts:
        review_parts = service.review_parts

        async def interrupted(*args, **kwargs):
            await review_parts(*args, **kwargs)
            raise SystemExit(77)

        service.review_parts = interrupted
        with pytest.raises(SystemExit):
            await service.translate_one(key)
    else:
        await service.translate_one(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["semantic", "legacy_unknown", "live_lease"])
@pytest.mark.parametrize("force", [False, True])
async def test_nonactionable_translation_cannot_starve_later_document_analysis(harness, monkeypatch, blocked, force):
    sessions, config, model = harness
    first, first_key = add_document(sessions, config, "recent", analyzed=True)
    second, second_key = add_document(sessions, config, "older", hours=1)
    if blocked == "semantic":
        await populate_translation(TranslationService(sessions, config.translation), first_key, approved=False)
    with sessions.begin() as session:
        row = session.get(Translation, first_key)
        row.retry_at = ""
        if blocked == "legacy_unknown":
            row.status = "error"  # No trustworthy call outcome exists for this historical row.
        if blocked == "live_lease":
            row.owner = "existing-worker"
            row.lease_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        later = session.get(Translation, second_key)
        later.status, later.title_zh, later.text_zh = "ready", CHINESE, CHINESE
    before = translation_snapshot(sessions, first_key)

    async def no_translation(*args, **kwargs):
        pytest.fail("Blocked work and an already ready sibling must not call translation models")

    monkeypatch.setattr(TranslationService, "request", no_translation)
    monkeypatch.setattr(TranslationService, "audit", no_translation)
    for iteration in range(2):
        service = ReadingService(sessions, config, TranslationService(sessions, config.translation))
        result = await service.pending(force=force)
        assert result == {"enabled": True, "fetched": 0, "summarized": 1 if iteration == 0 else 0}
    assert translation_snapshot(sessions, first_key) == before
    assert model.calls == ["ReadingOutput", "SummaryAuditOutput"]
    with sessions() as session:
        assert session.get(DocumentAnalysis, session.get(WebDocument, first).analysis_id).status == "ready"
        assert session.get(DocumentAnalysis, session.get(WebDocument, second).analysis_id).status == "ready"
        reviews = session.scalars(select(SummaryReview)).all()
        assert len(reviews) == 1 and reviews[0].status == "ready"


@pytest.mark.asyncio
async def test_translation_lane_finalizes_receipts_while_reading_leaves_them_untouched(harness, monkeypatch):
    sessions, config, model = harness
    _, key = add_document(sessions, config, "recent", analyzed=True)
    await populate_translation(TranslationService(sessions, config.translation), key, crash_after_receipts=True)
    with sessions.begin() as session:
        row = session.get(Translation, key)
        row.status = "running"
        row.attempts = config.translation.max_attempts
        row.retry_at = "2999-01-01"
        row.lease_until = ""
    before = translation_snapshot(sessions, key)

    async def no_translation(*args, **kwargs):
        pytest.fail("Completed review receipts must publish with zero model calls")

    monkeypatch.setattr(TranslationService, "request", no_translation)
    monkeypatch.setattr(TranslationService, "audit", no_translation)
    result = await ReadingService(sessions, config, TranslationService(sessions, config.translation)).pending()
    assert translation_snapshot(sessions, key) == before
    await TranslationService(sessions, config.translation).pending()
    after = translation_snapshot(sessions, key)
    assert result == {"enabled": True, "fetched": 0, "summarized": 0}
    assert after["status"] == "ready" and after["text_zh"] == CHINESE
    assert after["parts"] == before["parts"] and after["attempts"] == before["attempts"]
    assert model.calls == []


@pytest.mark.asyncio
async def test_analysis_lane_skips_translation_only_work_and_uses_original_evidence(harness, monkeypatch):
    sessions, config, model = harness
    _, waiting = add_document(sessions, config, "recent", analyzed=True)
    _, missing = add_document(sessions, config, "older", hours=1)
    _, ready = add_document(sessions, config, "oldest", hours=2)
    with sessions.begin() as session:
        session.delete(session.get(Translation, missing))
        row = session.get(Translation, ready)
        row.status, row.title_zh, row.text_zh = "ready", CHINESE, CHINESE
    before = {key: translation_snapshot(sessions, key) for key in (waiting, ready)}

    async def no_translation(*args, **kwargs):
        pytest.fail("The independent reading lane must not invoke translation")

    def no_reconcile(*args, **kwargs):
        pytest.fail("Read-only translation evidence must not update cached review flags")

    monkeypatch.setattr(TranslationService, "translate_one", no_translation)
    monkeypatch.setattr(TranslationService, "reconcile_machine_checks", no_reconcile)
    for _ in range(2):
        reader = ReadingService(sessions, config, TranslationService(sessions, config.translation))
        assert (await reader.pending(translate=False, limit=1))["summarized"] == 1
    assert {key: translation_snapshot(sessions, key) for key in before} == before
    with sessions() as session:
        assert session.get(Translation, missing) is None
    assert [source["url"] for source in model.sources] == ["https://example.org/older", "https://example.org/oldest"]
    assert "text_zh" not in model.sources[0]
    assert "text_zh" not in model.sources[1] and "title_zh" not in model.sources[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch_succeeds", [False, True])
async def test_small_reading_limit_bounds_unique_documents_and_failed_fetch_releases_next_pass(
    harness, monkeypatch, fetch_succeeds,
):
    sessions, config, model = harness
    config.reading.max_documents = 3
    first, _ = add_document(sessions, config, "recent")
    second, _ = add_document(sessions, config, "older", hours=1)
    with sessions.begin() as session:
        row = session.get(WebDocument, first)
        row.text, row.content_hash, row.retry_at = "", "", ""
    fetched = []

    async def fetch(self, url, **kwargs):
        fetched.append(url)
        if not fetch_succeeds:
            raise PageUnavailable("restricted", "合成网页暂时不可读取。")
        return url, 200, {"content-type": "text/plain"}, b"This synthetic AI model supports useful research. " * 5

    monkeypatch.setattr(PageFetcher, "bytes", fetch)
    reader = ReadingService(sessions, config, TranslationService(sessions, config.translation))
    assert await reader.pending(translate=False, limit=1) == {
        "enabled": True, "fetched": 1, "summarized": int(fetch_succeeds),
    }
    assert len(model.sources) == int(fetch_succeeds)
    if fetch_succeeds:
        assert model.sources[0]["url"] == "https://example.org/recent"
    with sessions() as session:
        assert session.get(DocumentAnalysis, session.get(WebDocument, second).analysis_id).status == "pending"
    assert await reader.pending(translate=False, limit=1) == {"enabled": True, "fetched": 0, "summarized": 1}
    assert fetched == ["https://example.org/recent"]
    assert model.sources[-1]["url"] == "https://example.org/older"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
async def test_invalid_reading_limit_is_rejected_before_work(harness, limit):
    sessions, config, model = harness
    reader = ReadingService(sessions, config, TranslationService(sessions, config.translation))
    with pytest.raises(ValueError, match="正整数"):
        await reader.pending(translate=False, limit=limit)
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expected", [
    ("fetch_due", True), ("analysis_due", True), ("published", False),
    ("analysis_backoff", False), ("unknown_summary", False), ("orphan", False),
])
async def test_reading_recovery_probe_is_read_only_and_excludes_nonactionable_work(harness, state, expected):
    sessions, config, model = harness
    uid, _ = add_document(sessions, config, "recent", analyzed=state in {"published", "analysis_backoff"})
    reader = ReadingService(sessions, config, TranslationService(sessions, config.translation))
    with sessions.begin() as session:
        document = session.get(WebDocument, uid)
        key = analysis_key(session, document, config)
        source = {"id": key, "url": document.final_url or document.url, "title": document.title,
                  "text": document.text, "partial": document.partial}
        if state in {"fetch_due", "orphan"}:
            document.text, document.retry_at = "", ""
        if state == "orphan":
            session.delete(session.get(ArticleDocument, ("recent", uid)))
        if state == "analysis_backoff":
            analysis = session.get(DocumentAnalysis, key)
            analysis.status, analysis.retry_at = "pending", "2999-01-01"
    if state == "unknown_summary":
        class UnknownProvider:
            async def complete(self, prompt, schema):
                raise RuntimeError("Synthetic unknown outcome")

        with pytest.raises(SummaryReviewPending):
            await reader.summary_reviews.analyze_documents(
                "document:" + key, [source], UnknownProvider(), evidence=document_review_evidence([source]),
            )
    statements = []
    engine = sessions.kw["bind"]

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.split()[0].upper())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        assert reader.has_pending() is expected
        assert reader.has_pending() is expected
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    assert not ({"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"} & set(statements))
    assert model.calls == []

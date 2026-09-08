"""Publication boundaries through real SQLite and the structured review service."""

import json
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from radar.api import create_app
from radar.config import Settings
from radar.models import (
    Article,
    ArticleDocument,
    Digest,
    DocumentAnalysis,
    Job,
    SummaryReview,
    WebDocument,
    now_iso,
)
from radar.pipeline import Pipeline, as_dict, digest_window
from radar.providers import StructuredProvider
from radar.reading import analysis_key, fingerprint, resource_views
from radar.summary_review import SummaryReviewPending
from radar.web_reader import PageFetcher

DAY = date(2020, 9, 8)
CANDIDATE = "私有候选标记不得通过读取接口公开"
ISSUE = "私有审核意见标记不得通过读取接口公开"
REPAIRED = "合成修订后的摘要内容"


def review_input(prompt):
    return json.loads(prompt.split("\nUNTRUSTED_REVIEW_INPUT:\n", 1)[1])


class ScriptedProvider(StructuredProvider):
    """Replace only model transport, leaving parsing, scope and review gates real."""

    def __init__(self):
        self.calls = []
        self.reject = "always"
        self.reject_ids = None
        self.hook = None

    async def complete(self, prompt, schema_type):
        name = schema_type.__name__
        if name == "DigestOutput":
            payload = json.loads(prompt.split("\nUNTRUSTED_SOURCE_DATA:\n", 1)[1])
            result = {
                "title": CANDIDATE,
                "overview": CANDIDATE,
                "stories": [{"title": CANDIDATE, "summary": CANDIDATE,
                             "why_it_matters": CANDIDATE, "category": "模型",
                             "source_ids": [row["id"]]} for row in payload["articles"][:8]],
            }
        elif name == "ReadingOutput":
            payload = json.loads(prompt.split("\nUNTRUSTED_DOCUMENTS:\n", 1)[1])
            result = {"documents": [document_candidate(row["id"]) for row in payload]}
        elif name == "SummaryAuditOutput":
            payload = review_input(prompt)
            result = {"audits": []}
            for unit in payload["units"]:
                targeted = self.reject_ids is None or bool(set(unit["source_ids"]) & self.reject_ids)
                failed = targeted and (self.reject == "always" or (
                    self.reject == "until_correction" and REPAIRED not in json.dumps(unit["candidate"], ensure_ascii=False)
                ))
                field = {"header": "overview", "story": "summary", "document": "summary_zh"}[unit["kind"]]
                result["audits"].append({
                    "unit_id": unit["unit_id"], "approved": not failed,
                    "issues": [{"field": field, "reason": ISSUE, "source_ids": unit["source_ids"]}] if failed else [],
                })
        elif name == "SummaryCorrections":
            payload = review_input(prompt)
            result = {"corrections": []}
            for unit in payload["units"]:
                candidate = deepcopy(unit["candidate"])
                for field in ("title", "overview", "summary", "why_it_matters", "title_zh", "summary_zh", "why_it_matters_zh"):
                    if field in candidate:
                        candidate[field] = REPAIRED
                if "key_points_zh" in candidate:
                    candidate["key_points_zh"] = [REPAIRED]
                result["corrections"].append({"unit_id": unit["unit_id"], "candidate": candidate})
        else:
            raise AssertionError(f"Unexpected structured output: {name}")
        self.calls.append((name, deepcopy(payload)))
        if self.hook:
            self.hook(name, payload)
        return json.dumps(result, ensure_ascii=False)

    def count(self, name):
        return sum(stage == name for stage, _ in self.calls)


def document_candidate(uid):
    return {"source_id": uid, "title_zh": CANDIDATE, "summary_zh": CANDIDATE,
            "key_points_zh": [CANDIDATE], "why_it_matters_zh": CANDIDATE}


@pytest.fixture
def make_harness(tmp_path, monkeypatch):
    engines = []
    model = ScriptedProvider()
    monkeypatch.setattr("radar.pipeline.make_provider", lambda _: model)
    monkeypatch.setattr("radar.reading.make_provider", lambda _: model)
    monkeypatch.setattr("radar.summary_review.make_provider", lambda _: model)

    async def no_fetch(*args, **kwargs):
        raise AssertionError("Saved-source review must not fetch pages")

    monkeypatch.setattr(PageFetcher, "bytes", no_fetch)

    def make(*, reading=False, max_documents=2, rounds=1, kind="command"):
        root = tmp_path / str(len(engines))
        root.mkdir()
        config_path = root / "config.toml"
        config_path.write_text(
            f'[provider]\nkind="{kind}"\nmodel="fixture-generator"\n'
            '[summary_review]\n'
            f'max_correction_rounds={rounds}\nmax_format_retries=0\n'
            '[translation]\nenabled=false\n'
            f'[reading]\nenabled={str(reading).lower()}\nmention_catalog={{}}\n'
            f'max_documents={max_documents}\n'
        )
        settings = Settings(_env_file=None, config_path=str(config_path),
                            database_url=f"sqlite:///{root / 'review.db'}",
                            reader_token="fixture-reader", admin_token="fixture-admin",
                            scheduler_enabled=False)
        app = create_app(settings)
        sessions = app.state.sessions
        engines.append(sessions.kw["bind"])
        return SimpleNamespace(app=app, sessions=sessions, pipeline=app.state.pipeline,
                               config=app.state.pipeline.config, model=model)

    yield make
    for engine in engines:
        engine.dispose()


def add_source(h, uid="article", *, web=False, hours=1):
    start, end = digest_window(DAY, h.config)
    url = f"https://example.org/{uid}"
    with h.sessions.begin() as session:
        article = Article(
            id=uid, platform="rss" if web else "x", source_id="fixture", external_id=uid,
            url=url, canonical_url=url, title="Synthetic AI research", text="Synthetic original AI evidence.",
            author="Synthetic source author", published_at=(end - timedelta(hours=hours)).isoformat(),
            topics=["模型"], score=50,
        )
        session.add(article)
        session.flush()
        key = None
        if web:
            key = fingerprint(url)
            title, text = "Synthetic saved page", f"Synthetic saved original evidence for {uid}."
            session.add(WebDocument(
                id=key, url=url, final_url=url, title=title, text=text, partial=False,
                content_hash=fingerprint(title, text, False), status="fetched",
                fetched_at=now_iso(), retry_at="2999-01-01",
            ))
            session.flush()
            session.add(ArticleDocument(article_id=uid, document_id=key, relation="source", label=""))
    return uid, key


def published_digest(h, uid):
    start, end = digest_window(DAY, h.config)
    with h.sessions.begin() as session:
        row = Digest(date=DAY.isoformat(), title="此前已发布的日报", overview="此前审核后的概览。",
                     stories=[{"title": "此前报道", "summary": "此前已发布的内容。", "why_it_matters": "此前价值说明。",
                               "category": "模型", "source_ids": [uid]}], provider="fixture",
                     window_start=start.isoformat(), window_end=end.isoformat(), source_count=1, coverage=[])
        session.add(row)
        session.flush()
        return deepcopy(as_dict(row))


async def public_responses(h, uid):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://fixture") as client:
        headers = {"Authorization": "Bearer fixture-reader"}
        routes = ("/v1/status", "/v1/digests", "/v1/digests/latest", f"/v1/digests/{DAY}",
                  "/v1/articles?q=Synthetic", f"/v1/articles/{uid}")
        responses = {route: await client.get(route, headers=headers) for route in routes}
    return responses


@pytest.mark.asyncio
async def test_rejected_digest_is_private_job_failed_and_api_has_no_candidate(make_harness):
    h = make_harness()
    uid, _ = add_source(h)
    assert h.config.summary_review.enabled is True
    job_id = await h.pipeline.run("digest", DAY)
    with h.sessions() as session:
        assert session.get(Digest, DAY.isoformat()) is None
        job = session.get(Job, job_id)
        assert job.status == "failed" and "已生成" not in job.message
        reviews = session.scalars(select(SummaryReview)).all()
        assert reviews and any(row.candidate for row in reviews)
    assert h.model.count("DigestOutput") == 1
    assert h.model.count("SummaryCorrections") > 0
    assert h.model.count("SummaryAuditOutput") >= 2
    responses = await public_responses(h, uid)
    assert responses["/v1/digests"].json() == {"items": []}
    assert responses["/v1/digests/latest"].status_code == 404
    for response in responses.values():
        assert CANDIDATE not in response.text and REPAIRED not in response.text and ISSUE not in response.text
        assert "generator_sources" not in response.text and "evidence_fingerprint" not in response.text


@pytest.mark.asyncio
async def test_failed_forced_digest_keeps_published_row_and_api_result(make_harness):
    h = make_harness()
    uid, _ = add_source(h)
    before = published_digest(h, uid)
    job_id = await h.pipeline.run("digest", DAY, force=True)
    with h.sessions() as session:
        assert as_dict(session.get(Digest, DAY.isoformat())) == before
        assert session.get(Job, job_id).status == "failed"
    responses = await public_responses(h, uid)
    assert responses["/v1/digests/latest"].json()["title"] == before["title"]
    assert all(CANDIDATE not in response.text and REPAIRED not in response.text for response in responses.values())
    calls = len(h.model.calls)
    h.pipeline = Pipeline(h.sessions, h.config)
    repeated = await h.pipeline.run("digest", DAY, force=True)
    assert len(h.model.calls) == calls
    with h.sessions() as session:
        assert as_dict(session.get(Digest, DAY.isoformat())) == before
        assert session.get(Job, repeated).status == "failed"


@pytest.mark.asyncio
async def test_correction_requires_fresh_audit_before_digest_publication(make_harness):
    h = make_harness()
    h.model.reject = "until_correction"
    add_source(h)
    assert await h.pipeline.digest(DAY) == DAY.isoformat()
    with h.sessions() as session:
        row = session.get(Digest, DAY.isoformat())
        assert row.title == REPAIRED and row.stories[0]["summary"] == REPAIRED
    stages = [name for name, _ in h.model.calls]
    correction = stages.index("SummaryCorrections")
    assert "SummaryAuditOutput" in stages[correction + 1:]
    count = len(stages)
    await h.pipeline.digest(DAY)
    assert len(h.model.calls) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["DigestOutput", "SummaryAuditOutput"])
@pytest.mark.parametrize("target", ["article", "direct_web"])
async def test_changed_original_during_generation_or_audit_never_publishes(make_harness, stage, target):
    h = make_harness(reading=target == "direct_web")
    h.model.reject = "never"
    uid, key = add_source(h, web=target == "direct_web")
    changed = False

    def change(name, payload):
        nonlocal changed
        if name != stage or changed:
            return
        changed = True
        with h.sessions.begin() as session:
            if target == "article":
                session.get(Article, uid).text = "Synthetic updated original article evidence."
            else:
                doc = session.get(WebDocument, key)
                doc.text = "Synthetic updated original web evidence."
                doc.content_hash = fingerprint(doc.title, doc.text, doc.partial)
                doc.analysis_id = ""

    h.model.hook = change
    with pytest.raises(SummaryReviewPending):
        await h.pipeline.digest(DAY)
    assert changed
    with h.sessions() as session:
        assert session.get(Digest, DAY.isoformat()) is None
    assert h.model.count("DigestOutput") == 1 and h.model.count("SummaryAuditOutput") > 0


@pytest.mark.asyncio
async def test_reading_rejection_is_visible_pending_without_exposing_candidate_or_old_ready_loss(make_harness):
    h = make_harness(reading=True)
    uid, key = add_source(h, "waiting", web=True)
    old_uid, old_key = add_source(h, "published", web=True, hours=2)
    with h.sessions.begin() as session:
        old_doc = session.get(WebDocument, old_key)
        old_doc.analysis_id = fingerprint(old_doc.content_hash, h.config.reading.revision)
        old = DocumentAnalysis(id=old_doc.analysis_id, status="ready", title_zh="已发布标题",
                               summary_zh="已发布摘要内容。", key_points_zh=["已发布重点。"],
                               why_it_matters_zh="已发布价值说明。", provider="fixture")
        session.add(old)
        session.flush()
        before = deepcopy(as_dict(old))
    result = await h.pipeline.reading.pending(force=True)
    assert result["summarized"] == 0
    with h.sessions() as session:
        analysis_id = session.get(WebDocument, key).analysis_id
        assert session.get(DocumentAnalysis, analysis_id).status == "review_required"
        assert as_dict(session.get(DocumentAnalysis, before["id"])) == before
        assert session.get(WebDocument, old_key).analysis_id == before["id"]
        assert len(session.scalars(select(SummaryReview)).all()) == 1
        views = resource_views(session, [uid, old_uid], h.config.translation, full=True)
    generated = [payload for stage, payload in h.model.calls if stage == "ReadingOutput"]
    assert len(generated) == 1 and [doc["id"] for doc in generated[0]] == [analysis_id]
    pending = views[uid][0]
    assert pending["status"] == "analysis_review_required" and "无需重新授权" in pending["message"]
    assert pending["summary_zh"] is None and pending["key_points_zh"] == []
    assert pending["title_zh"] is None and pending["why_it_matters_zh"] is None
    assert views[old_uid][0]["summary_zh"] == before["summary_zh"]
    responses = await public_responses(h, uid)
    assert all(CANDIDATE not in response.text and REPAIRED not in response.text and ISSUE not in response.text
               for response in responses.values())


@pytest.mark.asyncio
async def test_exhausted_first_document_does_not_starve_later_work_or_restart_force_rebill(make_harness):
    h = make_harness(reading=True, max_documents=1)
    first_uid, first_key = add_source(h, "first", web=True)
    _, second_key = add_source(h, "second", web=True, hours=2)
    with h.sessions() as session:
        first_doc = session.get(WebDocument, first_key)
        first_analysis = analysis_key(session, first_doc, h.config)
    h.model.reject_ids = {first_analysis}
    assert (await h.pipeline.reading.pending(force=True))["summarized"] == 0
    initial_calls = deepcopy(h.model.calls)
    h.pipeline = Pipeline(h.sessions, h.config)
    assert (await h.pipeline.reading.pending(force=True))["summarized"] == 1
    assert h.model.count("ReadingOutput") == 2
    with h.sessions() as session:
        first = session.get(DocumentAnalysis, session.get(WebDocument, first_key).analysis_id)
        second = session.get(DocumentAnalysis, session.get(WebDocument, second_key).analysis_id)
        assert first.status == "review_required" and second.status == "ready"
    assert h.model.calls[:len(initial_calls)] == initial_calls
    after = len(h.model.calls)
    for _ in range(2):
        h.pipeline = Pipeline(h.sessions, h.config)
        assert (await h.pipeline.reading.pending(force=True))["summarized"] == 0
    assert len(h.model.calls) == after
    responses = await public_responses(h, first_uid)
    assert responses[f"/v1/articles/{first_uid}"].json()["resources"][0]["status"] == "analysis_review_required"


@pytest.mark.asyncio
async def test_explicit_extractive_mode_remains_labelled_and_does_not_enter_review(make_harness, monkeypatch):
    from radar.providers import make_provider

    h = make_harness(kind="extractive")
    uid, _ = add_source(h)
    monkeypatch.setattr("radar.pipeline.make_provider", make_provider)
    assert h.config.summary_review.enabled is True
    assert await h.pipeline.digest(DAY) == DAY.isoformat()
    with h.sessions() as session:
        row = session.get(Digest, DAY.isoformat())
        assert row.provider == "extractive" and "来源摘录" in row.title and "尚未生成 AI 分析" in row.overview
        assert row.stories[0]["source_ids"] == [uid]
        assert session.scalars(select(SummaryReview)).all() == []
    assert h.model.calls == []


@pytest.mark.asyncio
async def test_nonforce_reuses_digest_published_by_other_session_during_audit(make_harness):
    h = make_harness()
    h.model.reject = "never"
    uid, _ = add_source(h)
    with h.sessions() as session:
        source_before = deepcopy(as_dict(session.get(Article, uid)))
    concurrent = {}

    def publish(name, payload):
        if name == "SummaryAuditOutput" and not concurrent:
            concurrent.update(published_digest(h, uid))

    h.model.hook = publish
    assert await h.pipeline.digest(DAY) == DAY.isoformat()
    assert concurrent and h.model.count("SummaryAuditOutput") > 0
    with h.sessions() as session:
        assert as_dict(session.get(Digest, DAY.isoformat())) == concurrent
        assert as_dict(session.get(Article, uid)) == source_before
    calls = len(h.model.calls)
    assert await h.pipeline.digest(DAY) == DAY.isoformat()
    assert len(h.model.calls) == calls


@pytest.mark.asyncio
async def test_force_cannot_overwrite_newer_digest_published_during_audit(make_harness):
    h = make_harness()
    h.model.reject = "never"
    uid, _ = add_source(h)
    before = published_digest(h, uid)
    with h.sessions() as session:
        source_before = deepcopy(as_dict(session.get(Article, uid)))
    concurrent = {}

    def publish(name, payload):
        if name == "SummaryAuditOutput" and not concurrent:
            with h.sessions.begin() as session:
                row = session.merge(Digest(**{
                    **deepcopy(before), "title": "另一任务刚发布的新版日报",
                    "overview": "并发发布已完成，旧任务不能覆盖。", "generated_at": now_iso(),
                }))
                session.flush()
                concurrent.update(deepcopy(as_dict(row)))

    h.model.hook = publish
    with pytest.raises(SummaryReviewPending):
        await h.pipeline.digest(DAY, force=True)
    assert concurrent and concurrent != before and h.model.count("SummaryAuditOutput") > 0
    with h.sessions() as session:
        assert as_dict(session.get(Digest, DAY.isoformat())) == concurrent
        assert as_dict(session.get(Article, uid)) == source_before
    responses = await public_responses(h, uid)
    assert responses["/v1/digests/latest"].json()["title"] == concurrent["title"]


@pytest.mark.asyncio
async def test_same_body_at_different_urls_needs_two_independent_reviews(make_harness):
    h = make_harness(reading=True)
    rejected_uid, rejected_key = add_source(h, "rejected-url", web=True)
    ready_uid, ready_key = add_source(h, "approved-url", web=True, hours=2)
    with h.sessions.begin() as session:
        rejected = session.get(WebDocument, rejected_key)
        ready = session.get(WebDocument, ready_key)
        ready.title, ready.text, ready.partial = rejected.title, rejected.text, rejected.partial
        ready.content_hash = rejected.content_hash
        rejected_analysis = analysis_key(session, rejected, h.config)
        ready_analysis = analysis_key(session, ready, h.config)
        assert rejected.content_hash == ready.content_hash and rejected.url != ready.url
        assert rejected_analysis != ready_analysis
    h.model.reject_ids = {rejected_analysis}
    result = await h.pipeline.reading.pending(force=True)
    assert result["summarized"] == 1 and h.model.count("ReadingOutput") == 2
    with h.sessions() as session:
        assert session.get(WebDocument, rejected_key).analysis_id == rejected_analysis
        assert session.get(WebDocument, ready_key).analysis_id == ready_analysis
        assert session.get(DocumentAnalysis, rejected_analysis).status == "review_required"
        assert session.get(DocumentAnalysis, ready_analysis).status == "ready"
        reviews = session.scalars(select(SummaryReview)).all()
        assert len(reviews) == 2
        assert {row.status for row in reviews} == {"ready", "review_required"}
        assert len({row.evidence_fingerprint for row in reviews}) == 2
        assert {row.evidence[0]["url"] for row in reviews} == {
            f"https://example.org/{rejected_uid}", f"https://example.org/{ready_uid}",
        }
        views = resource_views(session, [rejected_uid, ready_uid], h.config.translation, full=True)
    assert views[rejected_uid][0]["status"] == "analysis_review_required"
    assert views[rejected_uid][0]["summary_zh"] is None
    assert views[ready_uid][0]["status"] == "ready"
    assert views[ready_uid][0]["summary_zh"] == CANDIDATE


@pytest.mark.asyncio
async def test_new_url_cannot_inherit_another_documents_published_legacy_analysis(make_harness):
    h = make_harness(reading=True)
    old_uid, old_key = add_source(h, "legacy-url", web=True, hours=2)
    new_uid, new_key = add_source(h, "new-url", web=True)
    with h.sessions.begin() as session:
        old_doc = session.get(WebDocument, old_key)
        old_doc.analysis_id = fingerprint(old_doc.content_hash, h.config.reading.revision)
        published = DocumentAnalysis(
            id=old_doc.analysis_id, status="ready", title_zh="旧网址已发布标题",
            summary_zh="旧网址已发布的历史解读。", key_points_zh=["旧网址已保存重点。"],
            why_it_matters_zh="旧网址已保存价值。", provider="historical-provider",
        )
        session.add(published)
        session.flush()
        before = deepcopy(as_dict(published))
        new_doc = session.get(WebDocument, new_key)
        new_doc.title, new_doc.text, new_doc.partial = old_doc.title, old_doc.text, old_doc.partial
        new_doc.content_hash = old_doc.content_hash
        assert new_doc.analysis_id == ""
        assert analysis_key(session, old_doc, h.config) == before["id"]
        new_analysis = analysis_key(session, new_doc, h.config)
        assert new_analysis != before["id"]
    result = await h.pipeline.reading.pending(force=True)
    assert result["summarized"] == 0 and h.model.count("ReadingOutput") == 1
    with h.sessions() as session:
        assert as_dict(session.get(DocumentAnalysis, before["id"])) == before
        assert session.get(WebDocument, old_key).analysis_id == before["id"]
        assert session.get(WebDocument, new_key).analysis_id == new_analysis
        assert session.get(DocumentAnalysis, new_analysis).status == "review_required"
        reviews = session.scalars(select(SummaryReview)).all()
        assert len(reviews) == 1 and reviews[0].evidence[0]["url"] == f"https://example.org/{new_uid}"
        views = resource_views(session, [old_uid, new_uid], h.config.translation, full=True)
    assert views[old_uid][0]["status"] == "ready"
    assert views[old_uid][0]["summary_zh"] == before["summary_zh"]
    assert views[new_uid][0]["status"] == "analysis_review_required"
    assert views[new_uid][0]["summary_zh"] is None

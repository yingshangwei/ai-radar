"""Bounded summary slices persist real SQLite stages; all providers are synthetic."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_summary_review import Model, candidate, sources

from radar.article_presentation import ArticlePresentationService, presentation_views
from radar.config import ProviderConfig, RadarConfig
from radar.db import database
from radar.models import Article, ArticleDocument, DocumentAnalysis, SummaryReview, WebDocument
from radar.reading import ReadingService, analysis_key, fingerprint
from radar.summary_review import SummaryReviewPending, SummaryReviewService, SummaryReviewYield
from radar.translation import TranslationService
from radar.web_reader import PageFetcher


@pytest.fixture
def harness(tmp_path, monkeypatch):
    engine, sessions = database(f"sqlite:///{tmp_path / 'slices.db'}")
    config = RadarConfig(provider=ProviderConfig(kind="command"))
    model = Model()
    monkeypatch.setattr("radar.summary_review.make_provider", lambda _: model)
    yield sessions, config, model
    engine.dispose()


def snapshots(sessions):
    with sessions() as session:
        return {row.scope: {column.name: deepcopy(getattr(row, column.name))
                           for column in row.__table__.columns}
                for row in session.scalars(select(SummaryReview))}


def sliced(sessions, config):
    service = SummaryReviewService(sessions, config)
    service.stage_call_limit = 2
    return service


def assert_completed_calls(state):
    calls = {event["call_id"] for event in state["history"] if event["event"] == "call"}
    outcomes = {event["call_id"] for event in state["history"]
                if event["event"] != "call" and event.get("call_id")}
    assert calls == outcomes
    assert state["owner"] == state["lease_until"] == ""


@pytest.mark.asyncio
async def test_two_stage_slice_persists_draft_and_first_approval_then_restarts_at_remaining_unit(harness):
    sessions, config, model = harness
    service = sliced(sessions, config)
    material = sources("a", "b")
    with pytest.raises(SummaryReviewYield):
        await service.analyze_documents("batch", material, model)
    before = snapshots(sessions)["batch"]
    assert before["status"] == "pending" and before["failure_code"] == ""
    assert before["candidate"]["documents"] == [candidate("a"), candidate("b")]
    assert model.count("ReadingOutput") == model.count("SummaryAuditOutput") == 1
    assert_completed_calls(before)
    assert service._slice.get() is None

    restarted = sliced(sessions, config)
    assert restarted.can_analyze_documents("batch", material)
    result = await restarted.analyze_documents("batch", material, model)
    after = snapshots(sessions)["batch"]
    assert after["id"] == before["id"] and after["status"] == "ready"
    assert after["candidate"] == before["candidate"] == result.model_dump()
    assert after["history"][:len(before["history"])] == before["history"]
    assert model.count("ReadingOutput") == 1 and model.count("SummaryAuditOutput") == 2
    assert [event["unit_id"] for event in after["history"] if event["event"] == "audit"] == ["document:a", "document:b"]
    assert_completed_calls(after)
    await sliced(sessions, config).analyze_documents("batch", material, model)
    assert snapshots(sessions)["batch"] == after and len(model.calls) == 3


@pytest.mark.asyncio
async def test_rejected_slice_resumes_correction_then_fresh_audit_without_repeating_denial(harness):
    sessions, config, model = harness
    model.reject_ids = {"a"}
    with pytest.raises(SummaryReviewYield):
        await sliced(sessions, config).analyze_documents("correction", sources(), model)
    before = snapshots(sessions)["correction"]
    rejection = [event for event in before["history"] if event["event"] == "audit"]
    assert len(rejection) == 1 and not rejection[0]["audit"]["approved"]
    assert before["correction_rounds"] == {} and before["status"] == "pending"
    assert_completed_calls(before)

    await sliced(sessions, config).analyze_documents("correction", sources(), model)
    after = snapshots(sessions)["correction"]
    assert after["status"] == "ready" and after["correction_rounds"] == {"document:a": 1}
    audits = [event for event in after["history"] if event["event"] == "audit"]
    assert len(audits) == 2 and audits[1]["audit"]["approved"]
    assert audits[1]["fingerprint"] != audits[0]["fingerprint"]
    assert after["history"][:len(before["history"])] == before["history"]
    assert [name for name, _ in model.calls] == ["ReadingOutput", "SummaryAuditOutput", "SummaryCorrections", "SummaryAuditOutput"]
    assert_completed_calls(after)


@pytest.mark.asyncio
async def test_format_retry_consumes_slice_and_keeps_valid_draft_for_later_audit(harness):
    sessions, config, model = harness
    model.responses["ReadingOutput"] = ["invalid synthetic JSON"]
    with pytest.raises(SummaryReviewYield):
        await sliced(sessions, config).analyze_documents("format", sources(), model)
    before = snapshots(sessions)["format"]
    assert before["status"] == "pending" and before["candidate"]
    assert model.count("ReadingOutput") == 2 and model.count("SummaryAuditOutput") == 0
    assert_completed_calls(before)
    await sliced(sessions, config).analyze_documents("format", sources(), model)
    assert model.count("ReadingOutput") == 2 and model.count("SummaryAuditOutput") == 1
    assert snapshots(sessions)["format"]["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("unchanged", [False, True])
async def test_rebuilding_slices_or_removing_slice_limit_cannot_reset_semantic_budget(harness, unchanged):
    sessions, config, model = harness
    config.summary_review.max_correction_rounds = 1
    model.reject_ids, model.forever_reject, model.unchanged = {"a"}, True, unchanged
    for index in range(2):
        with pytest.raises(SummaryReviewPending) as caught:
            await sliced(sessions, config).analyze_documents("blocked", sources(), model)
        assert isinstance(caught.value, SummaryReviewYield) is (index == 0)
    before = snapshots(sessions)["blocked"]
    assert before["status"] == "review_required"
    assert before["failure_code"] == ("correction_unchanged" if unchanged else "correction_budget_exhausted")
    assert model.count("ReadingOutput") == 1 and model.count("SummaryCorrections") == 1
    assert model.count("SummaryAuditOutput") == (1 if unchanged else 2)
    calls = deepcopy(model.calls)
    unlimited = SummaryReviewService(sessions, config)
    assert not unlimited.can_analyze_documents("blocked", sources())
    with pytest.raises(SummaryReviewPending):
        await unlimited.analyze_documents("blocked", sources(), model)
    assert model.calls == calls and snapshots(sessions)["blocked"] == before


@pytest.mark.asyncio
async def test_unknown_reserved_call_stays_blocked_across_slice_recreation(harness):
    sessions, config, model = harness
    service = sliced(sessions, config)
    key, frozen, fingerprint = service._identity("documents", "unknown", sources(), None)
    state, owner = service._claim(key, "documents", "unknown", sources(), frozen, fingerprint)
    stage, target, unit_id = service._next_call(state)
    service._reserve(key, owner, stage, target, 60, unit_id, "synthetic request with unknown outcome")
    # Simulate an expired worker after its durable reservation, with no known result.
    with sessions.begin() as session:
        session.get(SummaryReview, key).lease_until = ""
    assert not sliced(sessions, config).can_analyze_documents("unknown", sources())
    with pytest.raises(SummaryReviewPending):
        await sliced(sessions, config).analyze_documents("unknown", sources(), model)
    after = snapshots(sessions)["unknown"]
    assert not model.calls and after["candidate"] == {}
    assert after["status"] == "error" and after["failure_code"] == "unknown_call_outcome"
    assert len([event for event in after["history"] if event["event"] == "call"]) == 1


@pytest.mark.asyncio
async def test_contextvar_gives_overlapping_calls_independent_slices(harness):
    sessions, config, model = harness
    service = sliced(sessions, config)
    both_entered = asyncio.Event()
    entered = 0

    async def barrier(name):
        nonlocal entered
        if name == "ReadingOutput":
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()

    model.hook = barrier
    result = await asyncio.wait_for(asyncio.gather(
        service.analyze_documents("left", sources("a", "b"), model),
        service.analyze_documents("right", sources("c", "d"), model),
        return_exceptions=True,
    ), timeout=5)
    assert all(isinstance(value, SummaryReviewYield) for value in result)
    assert model.count("ReadingOutput") == 2 and model.count("SummaryAuditOutput") == 2
    for before in snapshots(sessions).values():
        assert before["status"] == "pending"
        assert len([event for event in before["history"] if event["event"] == "audit"]) == 1
        assert_completed_calls(before)
    assert service._slice.get() is None
    model.hook = None
    await asyncio.gather(service.analyze_documents("left", sources("a", "b"), model),
                         service.analyze_documents("right", sources("c", "d"), model))
    assert model.count("ReadingOutput") == 2 and model.count("SummaryAuditOutput") == 4
    assert all(state["status"] == "ready" for state in snapshots(sessions).values())


@pytest.mark.asyncio
async def test_default_digest_is_not_sliced_and_single_document_can_finish_at_exact_limit(harness):
    sessions, config, model = harness
    service = SummaryReviewService(sessions, config)
    assert service.stage_call_limit is None
    result = await service.generate_digest("digest", sources("a", "b"), "2026-09-08", model)
    assert len(result.stories) == 2
    assert model.count("DigestOutput") == 1 and model.count("SummaryAuditOutput") == 3
    await sliced(sessions, config).analyze_documents("single", sources(), model)
    states = snapshots(sessions)
    assert states["digest"]["status"] == states["single"]["status"] == "ready"
    assert_completed_calls(states["single"])


@pytest.mark.asyncio
async def test_durable_global_budget_wins_over_slice_yield(harness):
    sessions, config, model = harness
    config.summary_review.max_calls = 2
    with pytest.raises(SummaryReviewPending) as caught:
        await sliced(sessions, config).analyze_documents("budget", sources("a", "b"), model)
    assert not isinstance(caught.value, SummaryReviewYield)
    state = snapshots(sessions)["budget"]
    assert state["status"] == "error" and state["failure_code"] == "global_call_budget_exhausted"
    assert len(model.calls) == 2 and not sliced(sessions, config).can_analyze_documents("budget", sources("a", "b"))


@pytest.mark.asyncio
async def test_saved_document_slice_is_immediately_actionable_without_refetch_or_regeneration(harness, monkeypatch):
    sessions, config, model = harness
    config.reading.enabled = True
    config.reading.max_documents = 1
    config.reading.mention_catalog = {}
    source = sources()[0]
    monkeypatch.setattr("radar.reading.make_provider", lambda _: model)

    async def no_fetch(*args, **kwargs):
        pytest.fail("Saved source must not be fetched again")

    monkeypatch.setattr(PageFetcher, "bytes", no_fetch)
    with sessions.begin() as session:
        session.add(Article(id="a", platform="rss", source_id="synthetic", external_id="a",
            canonical_url=source["url"], **{name: source[name] for name in ("url", "title", "text", "author")},
            published_at=datetime.now(UTC).isoformat()))
        doc = WebDocument(id=fingerprint(source["url"]), url=source["url"], final_url=source["url"],
            title=source["title"], text=source["text"], status="fetched", fetched_at=datetime.now(UTC).isoformat(),
            content_hash=fingerprint(source["title"], source["text"], False), retry_at="2999-01-01")
        session.add(doc)
        session.flush()
        key = analysis_key(session, doc, config)
        session.add(ArticleDocument(article_id="a", document_id=doc.id, relation="source"))
    model.reject_ids = {key}

    def reading():
        service = ReadingService(sessions, config, TranslationService(sessions, config.translation))
        service.summary_reviews.stage_call_limit = 2
        return service

    first = reading()
    assert (await first.pending())["summarized"] == 0
    with sessions() as session:
        analysis = session.get(DocumentAnalysis, key)
        assert analysis.status == "pending" and analysis.retry_at == ""
        assert not analysis.summary_zh
    assert first.has_pending()
    assert (await reading().pending())["summarized"] == 1
    with sessions() as session:
        analysis = session.get(DocumentAnalysis, key)
        assert analysis.status == "ready" and "修订" in analysis.summary_zh
    assert model.count("ReadingOutput") == 1 and model.count("SummaryAuditOutput") == 2
    assert model.count("SummaryCorrections") == 1


@pytest.mark.asyncio
async def test_presentation_limit_and_pure_pending_probe_skip_exhausted_newer_article(harness):
    sessions, config, model = harness
    config.summary_review.max_correction_rounds = 0
    now = datetime.now(UTC)
    with sessions.begin() as session:
        for index in range(3):
            uid = str(index)
            source = sources(uid)[0]
            session.add(Article(id=uid, platform="x", source_id="synthetic", external_id=uid,
                canonical_url=source["url"], **{key: source[key] for key in ("url", "title", "text", "author")},
                published_at=(now - timedelta(minutes=index)).isoformat()))
    factories = []
    service = ArticlePresentationService(sessions, config, lambda _: factories.append(1) or model)
    service.reviews.stage_call_limit = 2
    assert service.has_pending() and service.has_pending()
    assert snapshots(sessions) == {} and not factories and not model.calls
    model.reject_ids = {"0"}
    assert await service.pending(limit=1) == {"processed": 1, "ready": 0}
    before = snapshots(sessions)["article:0"]
    assert before["status"] == "review_required"
    assert service.has_pending() and snapshots(sessions)["article:0"] == before
    assert await service.pending(limit=1) == {"processed": 1, "ready": 1}
    assert set(snapshots(sessions)) == {"article:0", "article:1"}
    assert service.has_pending()
    assert await service.pending(limit=1) == {"processed": 1, "ready": 1}
    calls = deepcopy(model.calls)
    assert not service.has_pending()
    assert await service.pending(limit=1) == {"processed": 0, "ready": 0}
    assert model.calls == calls and len(factories) == 3
    with sessions() as session:
        articles = list(session.scalars(select(Article).order_by(Article.id)))
        views = presentation_views(session, articles, config)
        assert views["0"]["title_zh"] is None and views["0"]["status"] == "review_required"
        assert views["1"]["status"] == views["2"]["status"] == "ready"

"""Industry evidence/version and model lifecycle tests; no external calls."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from radar.config import RadarConfig
from radar.db import database
from radar.industry import IndustryService, put_evidence
from radar.industry_models import IndustryAssessment, IndustryEvidence, IndustryTracking
from radar.industry_sources import EvidenceInput, IndustrySourceError
from radar.models import Article, SourceState

NOW = datetime(2026, 9, 13, 4, 0, tzinfo=UTC)


class Replies:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def __call__(self, config):
        return self

    async def complete(self, text, schema):
        self.calls.append((text, schema))
        assert self.values, "Unexpected additional provider call"
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def evidence_input(**overrides):
    data = dict(source_id="nvidia-news", external_id="fixture-1",
                url="https://nvidianews.nvidia.com/news/fixture",
                title="GPU orders and capital expenditure", text="Revenue grew; GPU orders increased.",
                published_at=(NOW - timedelta(days=1)).isoformat(), published_precision="timestamp",
                kind="company_release", entity_ids=["nvidia"],
                metadata={"partial": False, "evidence_scope": "publisher_article"})
    return EvidenceInput(**(data | overrides))


def report(uid, **overrides):
    claim = {"text_zh": "已披露订单变化，仍需验证后续交付。", "source_ids": [uid]}
    data = {"state": "insufficient_evidence", "summary_zh": "仅有单一主体证据，行业趋势尚待验证。",
            "supporting": [claim], "opposing": [], "investment_implications": [],
            "watch_items": [claim], "unknowns": ["预期差未知，缺少市场一致预期。"], "horizon": "未来两季度"}
    return json.dumps(data | overrides, ensure_ascii=False)


def audit(approved=True):
    return json.dumps({"approved": approved, "citations_supported": approved,
                       "numbers_and_dates_correct": True, "uncertainty_preserved": True,
                       "no_invented_market_data": True,
                       "issues": [] if approved else ["来源数字口径需要保留。"]}, ensure_ascii=False)


@pytest.fixture
def state(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/industry.db")
    clock = [NOW]
    config = RadarConfig(provider={"kind": "command", "command": ["unused-test-command"]},
                         industry={"enabled": True})
    service = IndustryService(sessions, config, clock=lambda: clock[0])
    with service.transaction() as session:
        for theme in session.scalars(select(IndustryTracking)):
            theme.enabled = theme.id == "infrastructure"
        put_evidence(session, evidence_input(), "NVIDIA", clock=clock[0])
        uid = session.scalar(select(IndustryEvidence.id))
    yield service, uid, clock
    engine.dispose()


def assessment(service):
    with service.sessions() as session:
        return session.scalar(select(IndustryAssessment))


def theme(service):
    return next(row for row in service.overview()["themes"] if row["id"] == "infrastructure")


async def test_only_exact_independently_approved_candidate_is_public(state):
    service, uid, _ = state
    provider = Replies(report(uid), audit())
    service.provider_factory = provider
    assert (await service.analyze())["status"] == "pending"
    assert theme(service)["assessment"] is None
    assert assessment(service).candidate["summary_zh"]
    assert (await service.analyze())["status"] == "ready"
    public = theme(service)["assessment"]
    assert public["summary_zh"] == json.loads(report(uid))["summary_zh"]
    assert public["evidence_ids"] == [uid]
    assert "candidate" not in public and "history" not in public
    assert (await service.analyze())["processed"] == 0
    assert len(provider.calls) == 2


async def test_report_waits_for_the_current_free_collection_sweep(state):
    service, uid, _ = state
    provider = Replies(report(uid))
    service.provider_factory = provider
    async with service.collect_lock:
        assert not service.has_pending()
        assert (await service.analyze())["processed"] == 0
        assert assessment(service) is None
    assert (await service.analyze())["status"] == "pending"
    assert len(provider.calls) == 1


async def test_correction_requires_new_audit_and_stops_after_rejection(state):
    service, uid, _ = state
    provider = Replies(report(uid), audit(False), report(uid), audit(False))
    service.provider_factory = provider
    for _ in range(3):
        assert (await service.analyze())["status"] == "pending"
        assert theme(service)["assessment"] is None
    assert (await service.analyze())["status"] == "needs_attention"
    assert theme(service)["assessment"] is None
    assert assessment(service).corrections == 1
    assert assessment(service).failure_code == "audit_rejected"
    assert (await service.analyze())["processed"] == 0
    assert len(provider.calls) == 4


async def test_invalid_source_id_is_known_failure_and_is_never_published(state):
    service, _, _ = state
    provider = Replies(report("invented-evidence"))
    service.provider_factory = provider
    assert (await service.analyze())["status"] == "needs_attention"
    assert assessment(service).failure_code == "invalid_response"
    assert theme(service)["assessment"] is None
    assert not service.has_pending()
    assert len(provider.calls) == 1


async def test_unknown_provider_result_is_not_called_again_after_restart(state):
    service, _, clock = state
    provider = Replies(TimeoutError("synthetic remote timeout"))
    service.provider_factory = provider
    assert (await service.analyze())["status"] == "outcome_unknown"
    clock[0] += timedelta(days=2)
    restarted = IndustryService(service.sessions, service.config, provider_factory=provider, clock=lambda: clock[0])
    assert not restarted.has_pending()
    assert (await restarted.analyze())["processed"] == 0
    assert len(provider.calls) == 1


async def test_second_audit_timeout_does_not_reuse_first_audit_receipt(state):
    service, uid, _ = state
    provider = Replies(report(uid), audit(False), report(uid), TimeoutError("second audit outcome unknown"))
    service.provider_factory = provider
    for _ in range(3):
        assert (await service.analyze())["status"] == "pending"
    assert (await service.analyze())["status"] == "outcome_unknown"
    assert assessment(service).failure_code == "unconfirmed_provider_result"
    assert theme(service)["assessment"] is None
    assert len(provider.calls) == 4


async def test_daily_reservations_limit_survives_service_restart(state):
    service, uid, clock = state
    service.options.max_calls_per_day = 1
    provider = Replies(report(uid), audit())
    service.provider_factory = provider
    await service.analyze()
    restarted = IndustryService(service.sessions, service.config, provider_factory=provider, clock=lambda: clock[0])
    assert not restarted.has_pending()
    assert (await restarted.analyze())["processed"] == 0
    assert theme(restarted)["assessment"] is None
    clock[0] += timedelta(days=1)
    assert (await restarted.analyze())["status"] == "ready"
    assert len(provider.calls) == 2


async def test_parallel_analyze_does_not_duplicate_generation(state):
    service, uid, _ = state
    provider = Replies(report(uid), audit())
    service.provider_factory = provider
    results = await asyncio.gather(service.analyze(), service.analyze())
    assert sorted(row["status"] for row in results) == ["pending", "ready"]
    assert len(provider.calls) == 2
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(IndustryAssessment)) == 1


async def test_frozen_evidence_survives_new_source_revision_before_audit(state):
    service, uid, clock = state
    provider = Replies(report(uid), audit())
    service.provider_factory = provider
    await service.analyze()
    original = deepcopy(assessment(service).evidence)
    clock[0] += timedelta(hours=1)
    with service.transaction() as session:
        assert put_evidence(session, evidence_input(text="Corrected: GPU orders declined."), "NVIDIA",
                            clock=clock[0]) == 1
    assert (await service.analyze())["status"] == "ready"
    row = assessment(service)
    assert row.evidence == original and row.as_of == NOW.isoformat()
    assert uid in provider.calls[1][0] and "Corrected: GPU orders declined." not in provider.calls[1][0]
    with service.sessions() as session:
        assert session.get(IndustryEvidence, uid).text == original[0]["text"]


@pytest.mark.parametrize("stage", ["generation", "audit", "correction"])
def test_restart_recovers_persisted_current_response_without_model_call(state, stage):
    service, uid, clock = state
    response = audit() if stage == "audit" else report(uid)
    with service.transaction() as session:
        selected = service._next(session)
        row = IndustryAssessment(**selected, as_of=NOW.isoformat(), created_at=NOW.isoformat(),
                                 stage=stage, status="calling", owner="persisted-owner",
                                 lease_until=(NOW - timedelta(minutes=1)).isoformat(),
                                 candidate=json.loads(report(uid)),
                                 history=[{"event": "call_reserved", "stage": stage, "owner": "persisted-owner",
                                           "at": (NOW - timedelta(minutes=5)).isoformat()},
                                          {"event": "response", "stage": stage, "owner": "persisted-owner",
                                           "at": (NOW - timedelta(minutes=2)).isoformat(), "text": response}])
        session.add(row)
    provider = Replies()
    restarted = IndustryService(service.sessions, service.config, provider_factory=provider, clock=lambda: clock[0])
    row = assessment(restarted)
    assert row.status == ("ready" if stage == "audit" else "pending")
    assert row.stage == "audit"
    assert not provider.calls
    assert (theme(restarted)["assessment"] is not None) == (stage == "audit")


def test_restart_without_response_preserves_unknown_and_old_audit_cannot_resolve_new_call(state):
    service, _, clock = state
    with service.transaction() as session:
        selected = service._next(session)
        session.add(IndustryAssessment(**selected, as_of=NOW.isoformat(), created_at=NOW.isoformat(),
            stage="audit", status="calling", owner="second-audit",
            lease_until=(NOW - timedelta(minutes=1)).isoformat(),
            history=[{"event": "call_reserved", "stage": "audit", "owner": "first-audit",
                      "at": (NOW - timedelta(minutes=20)).isoformat()},
                     {"event": "response", "stage": "audit", "owner": "first-audit",
                      "at": (NOW - timedelta(minutes=19)).isoformat(), "text": audit()},
                     {"event": "call_reserved", "stage": "audit", "owner": "second-audit",
                      "at": (NOW - timedelta(minutes=5)).isoformat()}]))
    restarted = IndustryService(service.sessions, service.config, provider_factory=Replies(), clock=lambda: clock[0])
    assert assessment(restarted).status == "outcome_unknown"
    assert theme(restarted)["assessment"] is None


def test_version_history_deduplicates_without_rolling_back_current(state):
    service, first_id, clock = state
    with service.transaction() as session:
        assert put_evidence(session, evidence_input(), "NVIDIA", clock=clock[0]) == 0
        assert put_evidence(session, evidence_input(text="Revised GPU orders."), "NVIDIA", clock=clock[0]) == 1
        current = session.scalar(select(IndustryEvidence).where(IndustryEvidence.current.is_(True)))
        assert current.revision == 2
        second_id = current.id
        assert put_evidence(session, evidence_input(), "NVIDIA", clock=clock[0]) == 0
    with service.sessions() as session:
        assert session.get(IndustryEvidence, first_id).text == evidence_input().text
        assert not session.get(IndustryEvidence, first_id).current
        assert session.get(IndustryEvidence, second_id).current


@pytest.mark.parametrize("fallback_kind,scope", [("filing_notice", "filing_metadata"),
                                                ("company_release", "feed_summary")])
def test_temporary_metadata_fallback_does_not_replace_richer_current_evidence(state, fallback_kind, scope):
    service, first_id, clock = state
    with service.transaction() as session:
        put_evidence(session, evidence_input(text="GPU announcement", kind=fallback_kind,
            metadata={"partial": True, "evidence_scope": scope, "document_fetch": "unavailable",
                      "article_fetch": "unavailable"}), "NVIDIA", clock=clock[0])
    with service.sessions() as session:
        assert session.get(IndustryEvidence, first_id).current
        selected = service._select_evidence(session, "infrastructure")
        assert selected[0]["id"] == first_id


def test_future_timestamp_cannot_enter_current_assessment(state):
    service, _, clock = state
    with service.transaction() as session:
        put_evidence(session, evidence_input(url="https://nvidianews.nvidia.com/news/future",
            published_at=(clock[0] + timedelta(minutes=3)).isoformat()), "NVIDIA", clock=clock[0])
        selected = service._select_evidence(session, "infrastructure")
    assert all(datetime.fromisoformat(item["published_at"]) <= clock[0] for item in selected)


def test_reusing_sources_never_mutates_original_news_or_includes_x(state):
    service, _, clock = state
    with service.transaction() as session:
        for platform, source_id in [("rss", "anthropic"), ("x", "x:user"), ("web", "unregistered")]:
            session.add(Article(id=platform, platform=platform, source_id=source_id, external_id=platform,
                url=f"https://example.test/{platform}", canonical_url=f"https://example.test/{platform}",
                title="Enterprise AI model", text="Enterprise AI model source text.", author="Fixture",
                published_at=(clock[0] - timedelta(hours=1)).isoformat(), saved=True))
    assert service.reuse_free_articles() == 1
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 3
        assert all(row.saved and row.text == "Enterprise AI model source text."
                   for row in session.scalars(select(Article)))
        reused = session.scalars(select(IndustryEvidence).where(IndustryEvidence.source_id.like("radar:%"))).all()
        assert [row.source_id for row in reused] == ["radar:anthropic"]


async def test_disabled_service_does_not_collect_or_call_models(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/disabled.db")
    provider = Replies()
    service = IndustryService(sessions, RadarConfig(), provider_factory=provider, clock=lambda: NOW)
    try:
        assert not service.due() and not service.has_pending()
        assert (await service.collect())["checked"] == 0
        assert (await service.analyze())["processed"] == 0
        assert not provider.calls
    finally:
        engine.dispose()


@pytest.mark.parametrize("error,expected", [
    (IndustrySourceError("blocked", "private upstream"), "HTTP 401/403"),
    (IndustrySourceError("rate_limited", "private upstream"), "HTTP 429"),
    (ValueError("private upstream"), "未通过校验"),
])
async def test_source_errors_are_actionable_and_do_not_stop_other_sources(state, monkeypatch, error, expected):
    service, _, _ = state
    from radar import industry
    keys = ["cninfo-inspur", "nvidia-news"]
    monkeypatch.setattr(industry, "SOURCES", {key: industry.SOURCES[key] for key in keys})

    async def fetch(client, source, **kwargs):
        if source.id == keys[0]:
            raise error
        return []

    monkeypatch.setattr(industry, "fetch_source", fetch)
    result = await service.collect()
    assert result["checked"] == 2 and result["failed"] == 1
    with service.sessions() as session:
        failed = session.get(SourceState, "industry:" + keys[0])
        assert expected in failed.message and "private" not in failed.message
        assert session.get(SourceState, "industry:" + keys[1]).status == "healthy"


def test_domestic_evidence_survives_newer_global_volume(state):
    service, _, _ = state
    sessions = service.sessions
    with service.transaction() as db:
        for i in range(260):
            put_evidence(db, evidence_input(url=f'https://nvidianews.nvidia.com/news/{i}',
                external_id=str(i)), 'NVIDIA', clock=NOW)
        put_evidence(db, evidence_input(url='https://static.cninfo.com.cn/finalpage/2026-08-20/123.PDF',
            source_id='cninfo-inspur', published_at=(NOW-timedelta(days=10)).isoformat(),
            entity_ids=['inspur'],metadata={'partial':False,'region':'cn','evidence_scope':'filing_primary_document'}),
            '巨潮 · 浪潮信息', clock=NOW)
    with sessions() as db:
        chosen=service._select_evidence(db,'infrastructure')
    assert any('inspur' in row['entity_ids'] for row in chosen)

"""Local SQLite lifecycle checks; no actual CLI or model calls are permitted."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import radar.discovery as discovery
from radar.config import RadarConfig
from radar.db import database
from radar.discovery import DiscoveryService, queue_candidate
from radar.models import DiscoveryCall, DiscoveryCandidate
from radar.schemas import IncomingArticle


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    engine, sessions = database(f"sqlite:///{tmp_path}/lifecycle.db")
    config = RadarConfig(provider={"kind": "command"}, discovery={"enabled": True, "max_pending": 1})
    instant = datetime.now(UTC)

    class Clock(datetime):
        elapsed = timedelta()

        @classmethod
        def now(cls, tz=None):
            value = instant + cls.elapsed
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(discovery, "datetime", Clock)
    monkeypatch.setattr(discovery, "now_iso", lambda: Clock.now(UTC).isoformat())

    def prohibit(*_args, **_kwargs):
        raise AssertionError("Lifecycle maintenance attempted a model call")

    monkeypatch.setattr(discovery, "make_provider", prohibit)
    service = DiscoveryService(sessions, config, prohibit)
    yield service, sessions, config, Clock
    engine.dispose()


def enqueue(lifecycle, uid="1"):
    _, sessions, config, clock = lifecycle
    item = IncomingArticle(platform="x", external_id=uid, url=f"https://x.com/research/status/{uid}",
        title="Synthetic AI benchmark release", author="Synthetic researcher", handle="research",
        text="We release an AI benchmark with reproducible tasks and documented experimental results. "
             "The research includes the evaluation dataset and its limitations.",
        published_at=clock.now(UTC), metrics={"like_count": 1})
    with sessions.begin() as session:
        key = queue_candidate(session, item, config).id
    return key, item


def snapshot(sessions):
    with sessions() as session:
        return {model.__tablename__: [
            {column.name: deepcopy(getattr(row, column.name)) for column in model.__table__.columns}
            for row in session.scalars(select(model).order_by(model.id))
        ] for model in (DiscoveryCandidate, DiscoveryCall)}


@pytest.mark.parametrize("state", ["pending", "retry_wait"])
def test_expired_uncalled_work_schedules_read_only_maintenance_without_new_input(lifecycle, state):
    service, sessions, _, clock = lifecycle
    key, original = enqueue(lifecycle)
    if state == "retry_wait":
        with sessions.begin() as session:
            row = session.get(DiscoveryCandidate, key)
            row.status, row.retry_at = state, (clock.now(UTC) + timedelta(days=3)).isoformat()
            session.add(DiscoveryCall(candidate_id=key, fingerprint=row.fingerprint, owner="old-owner",
                status="failed", error_code="rate_limited", provider={}, completed_at=clock.now(UTC).isoformat()))
    clock.elapsed = timedelta(hours=49)
    # Reobserving exactly the same source is not new content and does not repair the queue itself.
    with sessions.begin() as session:
        assert queue_candidate(session, original, service.config).id == key
    before = snapshot(sessions)
    assert service.status()["pending_limit_reached"]
    assert service.has_pending()
    assert snapshot(sessions) == before
    assert service.recover() == 1
    after = snapshot(sessions)
    assert after["discovery_candidates"][0]["status"] == "expired"
    assert after["discovery_candidates"][0]["error_code"] == "source_expired"
    assert after["discovery_candidates"][0]["payload"] == before["discovery_candidates"][0]["payload"]
    assert after["discovery_calls"] == before["discovery_calls"]
    assert not service.has_pending() and not service.status()["pending_limit_reached"]
    assert service.recover() == 0
    assert snapshot(sessions) == after
    assert enqueue(lifecycle, "2")[0] != key


async def test_stale_legacy_lease_schedules_unknown_receipt_finalization_without_replay(lifecycle):
    service, sessions, _, clock = lifecycle
    key, _ = enqueue(lifecycle)
    reservation = service._reserve(key)
    assert reservation
    before = snapshot(sessions)
    clock.elapsed = timedelta(seconds=service.provider_config.timeout_seconds + 61)
    assert service.has_pending()
    assert snapshot(sessions) == before
    assert service.recover() == 1
    with sessions() as session:
        candidate = session.get(DiscoveryCandidate, key)
        call = session.scalar(select(DiscoveryCall))
        assert candidate.status == call.status == "unknown"
        assert candidate.error_code == call.error_code == "outcome_unknown"
        assert not candidate.owner and not candidate.lease_until
        assert candidate.payload == before["discovery_candidates"][0]["payload"]
        assert call.fingerprint == before["discovery_calls"][0]["fingerprint"]
        assert call.owner == before["discovery_calls"][0]["owner"]
    finalized = snapshot(sessions)
    assert not service.has_pending()
    assert (await service.pending())["processed"] == 0
    assert service.recover() == 0
    assert snapshot(sessions) == finalized
    # A new service/process still treats the original request as uncertain.
    replacement = DiscoveryService(sessions, service.config, service.publish_callback)
    assert not replacement.has_pending()
    assert (await replacement.pending())["processed"] == 0


def test_live_legacy_lease_is_not_a_maintenance_or_replay_candidate(lifecycle):
    service, sessions, _, _ = lifecycle
    key, _ = enqueue(lifecycle)
    assert service._reserve(key)
    before = snapshot(sessions)
    assert not service.has_pending()
    assert service.recover() == 0
    assert snapshot(sessions) == before


@pytest.mark.parametrize("state", ["reserved", "pending", "retry_wait"])
def test_legacy_recovery_does_not_take_ownership_of_persistent_batch_members(lifecycle, state):
    service, sessions, _, clock = lifecycle
    key, _ = enqueue(lifecycle)
    assert service._reserve(key)
    with sessions.begin() as session:
        call = session.scalar(select(DiscoveryCall))
        call.provider = {**call.provider, "batch_id": "synthetic-batch-owned-elsewhere"}
        session.get(DiscoveryCandidate, key).status = state
    clock.elapsed = timedelta(hours=49)
    before = snapshot(sessions)
    assert service._recover_legacy() == 0
    assert snapshot(sessions) == before


async def test_pending_maintenance_releases_capacity_then_processes_fresh_work(lifecycle, monkeypatch):
    service, sessions, _, clock = lifecycle
    old_key, _ = enqueue(lifecycle)
    clock.elapsed = timedelta(hours=49)
    # The normal pending entry must run the same maintenance, without a new model call.
    assert service.has_pending()
    assert (await service.pending())["processed"] == 0
    with sessions() as session:
        assert session.get(DiscoveryCandidate, old_key).status == "expired"
    fresh_key, _ = enqueue(lifecycle, "2")
    called, applied = [], []

    class FakeProvider:
        async def complete(self, prompt, schema):
            candidate = json.loads(prompt.split("\nUNTRUSTED_CANDIDATE:\n")[1])
            called.append(candidate["candidate_id"])
            return json.dumps({"candidate_id": candidate["candidate_id"], "is_ai_relevant": True,
                "novelty": 70, "specificity": 80, "potential_impact": 70, "confidence": 75,
                "should_surface": False, "reason_zh": "研究提供实验结果，重要性仍需跟踪。",
                "uncertainty_zh": "尚无独立复现实验。", "evidence_quotes": ["reproducible tasks"],
                "entities": [], "named_entities": []})

    monkeypatch.setattr(discovery, "make_provider", lambda _: FakeProvider())
    service.publish_callback = lambda session, row: applied.append(row.id)
    assert (await service.pending())["judged"] == 1
    assert called == applied == [fresh_key]
    assert not service.has_pending()

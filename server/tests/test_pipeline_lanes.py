"""Actual managed Pipeline routing with synthetic services and an isolated DB."""

import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from radar.config import RadarConfig
from radar.db import database
from radar.models import Article, Digest, Job, Translation
from radar.pipeline import Pipeline
from radar.schemas import DigestOutput
from radar.translation import ensure_translation


@pytest.fixture
def pipeline(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/lanes.db")
    config = RadarConfig(provider={"kind": "extractive"}, translation={"enabled": True},
                         reading={"enabled": True}, anthropic_news_enabled=False)
    value = Pipeline(sessions, config)
    yield value
    engine.dispose()


def managed_job(pipeline, kind):
    with pipeline.sessions.begin() as session:
        job = Job(kind=kind, status="running", owner="fixture-owner",
                  queued_at=datetime.now(UTC).isoformat(), attempt=1)
        session.add(job)
        session.flush()
        return job.id


def saved_job(pipeline, uid):
    with pipeline.sessions() as session:
        return session.get(Job, uid)


async def no_model(*args, **kwargs):
    pytest.fail("This lane must not invoke another service or a model")


async def test_managed_collect_does_not_wait_for_read_or_translate_locks(pipeline, monkeypatch):
    calls, phases = [], []

    async def collect():
        calls.append("collect")
        return 3

    async def phase(value):
        phases.append(value)

    monkeypatch.setattr(pipeline, "collect", collect)
    monkeypatch.setattr(pipeline.translations, "pending", no_model)
    monkeypatch.setattr(pipeline.reading, "pending", no_model)
    monkeypatch.setattr(pipeline.presentations, "pending", no_model)
    uid = managed_job(pipeline, "collect")
    async with pipeline.lock, pipeline.translate_lock:
        result = await asyncio.wait_for(pipeline.run(kind="collect", job_id=uid, phase_callback=phase), 1)
    assert result == uid and calls == ["collect"] and phases == ["collect"]
    assert saved_job(pipeline, uid).status == "completed"
    assert not saved_job(pipeline, uid).more_pending


async def test_managed_translation_uses_short_batch_and_persists_continuation(pipeline, monkeypatch):
    calls, phases = [], []

    async def pending(**kwargs):
        calls.append(kwargs)
        return {"counts": {"ready": 1, "pending": 1}}

    async def phase(value):
        phases.append(value)

    monkeypatch.setattr(pipeline.translations, "pending", pending)
    monkeypatch.setattr(pipeline.translations, "has_pending", lambda: True)
    monkeypatch.setattr(pipeline.reading, "pending", no_model)
    monkeypatch.setattr(pipeline.presentations, "pending", no_model)
    uid = managed_job(pipeline, "translate")
    async with pipeline.lock, pipeline.collect_lock:
        result = await asyncio.wait_for(pipeline.run(
            kind="translate", force=True, job_id=uid, phase_callback=phase,
        ), 1)
    assert result == uid and phases == ["translate"]
    assert calls == [{"force": True, "limit": 2, "max_stage_calls": 2}]
    assert saved_job(pipeline, uid).status == "completed" and saved_job(pipeline, uid).more_pending


@pytest.mark.parametrize("kind", ["read", "present"])
@pytest.mark.parametrize("failure", [False, True])
async def test_separate_read_and_presentation_lanes_restore_stage_limits(pipeline, monkeypatch, failure, kind):
    calls, phases = [], []

    async def read(**kwargs):
        calls.append(("read", kwargs))
        assert pipeline.reading.summary_reviews.stage_call_limit == 2
        if failure:
            raise TimeoutError("synthetic terminated read timeout")
        return {"fetched": 1, "summarized": 0}

    async def presentation(**kwargs):
        calls.append(("presentation", kwargs))
        assert pipeline.presentations.reviews.stage_call_limit == 2
        if failure:
            raise TimeoutError("synthetic terminated presentation timeout")
        return {"processed":2,"ready":1}

    async def phase(value):
        phases.append(value)

    monkeypatch.setattr(pipeline.reading, "pending", read)
    monkeypatch.setattr(pipeline.presentations, "pending", presentation)
    monkeypatch.setattr(pipeline.translations, "pending", no_model)
    monkeypatch.setattr(pipeline.reading, "has_pending", lambda: False)
    monkeypatch.setattr(pipeline.presentations, "has_pending", lambda: False)
    uid = managed_job(pipeline, kind)
    run = pipeline.run(kind=kind, force=True, job_id=uid, phase_callback=phase)
    if failure:
        with pytest.raises(TimeoutError):
            await run
        assert saved_job(pipeline, uid).status == "running"  # Supervisor owns failure/retry disposition.
    else:
        assert await run == uid
        assert saved_job(pipeline, uid).status == "completed"
    if kind == "read":
        assert calls == [("read", {"force": True, "translate": False, "limit": 1})]
        assert phases == ["read"]
    else:
        assert calls == [("presentation", {"limit": 2})]
        assert phases == ["presentation"]
    assert pipeline.reading.summary_reviews.stage_call_limit is None
    assert pipeline.presentations.reviews.stage_call_limit is None


@pytest.mark.parametrize("kind", ["digest", "daily"])
async def test_managed_digest_uses_originals_without_starting_or_reading_translation(pipeline, monkeypatch, kind):
    day, generated, phases, collected = date(2020, 9, 8), [], [], []
    with pipeline.sessions.begin() as session:
        for name in ("ready", "uncached"):
            text = f"Synthetic AI research for {name}."
            session.add(Article(id=name, platform="rss", source_id="fixture", external_id=name,
                                url=f"https://example.org/{name}", canonical_url=f"https://example.org/{name}",
                                title=text, text=text, author="Fixture", published_at="2020-09-07T12:00:00+00:00"))
            if name == "ready":
                cache = ensure_translation(session, text, text, pipeline.config.translation)
                cache.status, cache.title_zh, cache.text_zh = "ready", "合成研究", "这是一份合成研究。"

    def snapshot():
        with pipeline.sessions() as session:
            return {row.id: deepcopy({column.name: getattr(row, column.name)
                                      for column in row.__table__.columns})
                    for row in session.scalars(select(Translation))}

    async def generate(sources, day):
        generated.append(deepcopy(sources))
        return DigestOutput(title="合成日报", overview="合成测试摘要。", stories=[{
            "title": "合成研究进展", "summary": "研究人员展示了测试结果。",
            "why_it_matters": "用于验证后台队列。", "category": "技术",
            "source_ids": [item["id"] for item in sources],
        }])

    async def collect():
        collected.append(True)
        return 0

    async def phase(value):
        phases.append(value)

    monkeypatch.setattr("radar.pipeline.make_provider", lambda _: SimpleNamespace(generate=generate))
    monkeypatch.setattr(pipeline.translations, "pending", no_model)
    monkeypatch.setattr(pipeline.translations, "translate_one", no_model)
    monkeypatch.setattr(pipeline.translations, "request", no_model)
    monkeypatch.setattr(pipeline.translations, "audit", no_model)
    monkeypatch.setattr(pipeline.translations, "reconcile_machine_checks", lambda *_: pytest.fail("No cache edits"))
    monkeypatch.setattr(pipeline.reading, "pending", no_model)
    monkeypatch.setattr(pipeline.presentations, "pending", no_model)
    monkeypatch.setattr(pipeline, "collect", collect)
    before = snapshot()
    uid = managed_job(pipeline, kind)
    async with pipeline.translate_lock:
        assert await asyncio.wait_for(pipeline.run(
            kind=kind, day=day, job_id=uid, phase_callback=phase,
        ), 1) == uid
    assert snapshot() == before
    assert len(generated) == 1
    sources = {item["id"]: item for item in generated[0]}
    assert "text_zh" not in sources["ready"]
    assert "text_zh" not in sources["uncached"]
    assert collected == ([True] if kind == "daily" else [])
    assert phases == (["collect", "digest"] if kind == "daily" else ["digest"])
    with pipeline.sessions() as session:
        digest = session.get(Digest, day.isoformat())
        assert digest.source_count == 2 and set(digest.stories[0]["source_ids"]) == {"ready", "uncached"}


async def test_real_managed_presentation_does_not_acquire_web_processing_lock(pipeline, monkeypatch):
    calls = []
    async def pending(**kwargs):
        calls.append(kwargs)
        return {"processed": 1, "ready": 1}
    async def phase(value):
        assert value == "presentation"
    monkeypatch.setattr(pipeline.presentations, "pending", pending)
    monkeypatch.setattr(pipeline.presentations, "has_pending", lambda: False)
    monkeypatch.setattr(pipeline.reading, "pending", no_model)
    uid = managed_job(pipeline, "present")
    async with pipeline.lock, pipeline.translate_lock, pipeline.discover_lock:
        assert await asyncio.wait_for(pipeline.run(kind="present", job_id=uid, phase_callback=phase), 1) == uid
    assert calls == [{"limit": 2}] and saved_job(pipeline, uid).status == "completed"

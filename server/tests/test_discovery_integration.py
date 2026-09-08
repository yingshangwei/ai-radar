"""Real SQLite/service routing with synthetic decisions; no network or live model."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from radar import api, cli
from radar.config import RadarConfig, Settings
from radar.db import database
from radar.discovery_watches import article_signal
from radar.jobs import JobSupervisor
from radar.models import (
    Article,
    DiscoveryCall,
    DiscoveryCandidate,
    DiscoveryEntity,
    DiscoveryWatch,
    Job,
    Watch,
)
from radar.pipeline import Pipeline, ingest
from radar.schemas import IncomingArticle
from radar.x_collection import XCollectionResult


def item(uid="1", **changes):
    return IncomingArticle(**{
        "platform": "x", "external_id": uid, "url": f"https://x.com/testresearch/status/{uid}",
        "title": "Synthetic AI benchmark release", "author": "Test researcher", "handle": "testresearch",
        "text": "We release an AI benchmark with reproducible tasks and a documented evaluation dataset. "
                "The research contains experimental evidence and describes current limitations.",
        "published_at": datetime.now(UTC), "metrics": {"like_count": 1}, **changes,
    })


class Model:
    def __init__(self):
        self.calls = []
        self.surface = True

    async def complete(self, prompt, schema):
        payload = json.loads(prompt.split("\nUNTRUSTED_CANDIDATE:\n")[1])
        self.calls.append(payload["candidate_id"])
        return json.dumps({
            "candidate_id": payload["candidate_id"], "is_ai_relevant": True,
            "novelty": 90, "specificity": 90, "potential_impact": 90, "confidence": 90,
            "should_surface": self.surface, "reason_zh": "这是基于可复现实验的潜力预判，仍需进一步验证。",
            "uncertainty_zh": "目前尚缺少独立复现实验。", "evidence_quotes": ["reproducible tasks"],
            "entities": [], "named_entities": [],
        }, ensure_ascii=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine, sessions = database(f"sqlite:///{tmp_path}/integration.db")
    config = RadarConfig(anthropic_news_enabled=False, min_engagement=30,
                         provider={"kind": "command"}, discovery={"enabled": True},
                         translation={"enabled": False}, reading={"enabled": False})
    model = Model()
    monkeypatch.setattr("radar.discovery.make_provider", lambda _: model)
    pipeline = Pipeline(sessions, config)
    yield pipeline, model
    engine.dispose()


def enqueue(pipeline, value):
    with pipeline.sessions.begin() as session:
        return ingest(session, [value], pipeline.config)


def rows(pipeline, model):
    with pipeline.sessions() as session:
        return session.scalars(select(model)).all()


async def test_low_heat_queues_before_filter_then_only_real_saved_decision_publishes(env):
    pipeline, model = env
    original = item()
    assert enqueue(pipeline, original) == 0
    assert len(rows(pipeline, DiscoveryCandidate)) == 1 and rows(pipeline, Article) == []
    with pipeline.sessions.begin() as session:
        assert ingest(session, [original], pipeline.config, approved_signal="0" * 64) == 0
    uid = await pipeline.run("discover")
    assert len(model.calls) == 1
    saved = rows(pipeline, Article)[0]
    assert saved.text == original.text and saved.title == original.title and saved.topics[0] == "前瞻"
    assert rows(pipeline, Job)[0].id == uid and rows(pipeline, Job)[0].status == "completed"
    with pipeline.sessions() as session:
        assert article_signal(session, session.get(Article, saved.id))["label"] == "潜力预判"
    await pipeline.run("discover", force=True)
    assert len(model.calls) == 1 and len(rows(pipeline, DiscoveryCandidate)) == 1


async def test_negative_surface_decision_does_not_bypass_engagement(env):
    pipeline, model = env
    model.surface = False
    enqueue(pipeline, item())
    await pipeline.run("discover")
    assert len(model.calls) == 1 and rows(pipeline, Article) == []


async def test_existing_signal_updates_real_metrics_but_changed_low_heat_source_does_not_inherit_it(env):
    pipeline, model = env
    original = item()
    enqueue(pipeline, original)
    await pipeline.run("discover")
    updated = original.model_copy(update={"metrics": {"like_count": 5}})
    assert enqueue(pipeline, updated) == 0
    saved = rows(pipeline, Article)[0]
    assert saved.metrics == {"like_count": 5} and "前瞻" in saved.topics
    changed = original.model_copy(update={"text": original.text + " A different substantive claim."})
    assert enqueue(pipeline, changed) == 0
    assert rows(pipeline, Article)[0].text == original.text and len(model.calls) == 1
    assert len(rows(pipeline, DiscoveryCandidate)) == 2


def test_non_ai_content_never_enters_discovery_queue(env):
    pipeline, _ = env
    assert enqueue(pipeline, item(title="A birthday party", text="Happy birthday and enjoy your weekend!")) == 0
    assert rows(pipeline, DiscoveryCandidate) == []


async def test_managed_discover_has_independent_lane_and_never_runs_translation_or_reading(env, monkeypatch):
    pipeline, model = env
    enqueue(pipeline, item())

    async def forbidden(*args, **kwargs):
        pytest.fail("discover must not run another content lane")

    for service in (pipeline.translations, pipeline.reading, pipeline.presentations):
        monkeypatch.setattr(service, "pending", forbidden)
    with pipeline.sessions.begin() as session:
        job = Job(kind="discover", status="running", owner="test-owner", queued_at=datetime.now(UTC).isoformat())
        session.add(job)
        session.flush()
        uid = job.id
    phases = []

    async def phase(value):
        phases.append(value)

    async with pipeline.lock, pipeline.translate_lock, pipeline.collect_lock:
        await asyncio.wait_for(pipeline.run("discover", job_id=uid, phase_callback=phase), 1)
    assert phases == ["discover"] and len(model.calls) == 1
    assert rows(pipeline, Job)[0].status == "completed"


async def test_legacy_discover_is_bounded_and_does_not_run_enabled_other_services(env, monkeypatch):
    pipeline, model = env
    pipeline.config.translation.enabled = pipeline.config.reading.enabled = True

    async def forbidden(*args, **kwargs):
        pytest.fail("legacy discover must not launch translation, reading or presentation")

    for service in (pipeline.translations, pipeline.reading, pipeline.presentations):
        monkeypatch.setattr(service, "pending", forbidden)
    for index in range(3):
        enqueue(pipeline, item(str(index)))
    await pipeline.run("discover")
    assert len(model.calls) == 2 and pipeline.has_pending("discover")


def test_pipeline_startup_recovers_expired_unknown_without_a_new_model_call(env):
    pipeline, model = env
    enqueue(pipeline, item())
    candidate = rows(pipeline, DiscoveryCandidate)[0]
    pipeline.discovery._reserve(candidate.id)
    with pipeline.sessions.begin() as session:
        session.get(DiscoveryCandidate, candidate.id).lease_until = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    restarted = Pipeline(pipeline.sessions, pipeline.config)
    assert rows(restarted, DiscoveryCandidate)[0].status == "unknown"
    assert rows(restarted, DiscoveryCall)[0].status == "unknown"
    assert not restarted.has_pending("discover") and model.calls == []


async def test_supervisor_maintains_coalesced_discover_intent_in_its_own_lane(env):
    pipeline, model = env
    enqueue(pipeline, item())
    supervisor = JobSupervisor(pipeline, automatic=False)
    supervisor.stopping = True
    try:
        supervisor._enqueue_eligible()
        supervisor._enqueue_eligible()
        pending = [job for job in rows(pipeline, Job) if job.kind == "discover"]
        assert len(pending) == 1
        with pipeline.sessions.begin() as session:
            session.add(Job(kind="read", status="running", owner="busy-processing-owner"))
        claimed = supervisor._claim("discover")
        assert claimed[0] == pending[0].id
        await supervisor._phase(claimed[0], "discover")
        assert model.calls == []
    finally:
        await supervisor.stop()


async def test_api_discovery_auth_scope_status_topic_and_import_wakeup(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('anthropic_news_enabled=false\n[provider]\nkind="command"\n'
                      '[discovery]\nenabled=true\n[translation]\nenabled=false\n[reading]\nenabled=false\n')
    app = api.create_app(Settings(config_path=str(config), database_url=f"sqlite:///{tmp_path}/api.db",
                                  reader_token="reader", admin_token="admin", scheduler_enabled=False))
    supervisor = app.state.supervisor
    supervisor.stopping = True
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            assert (await client.get("/v1/discovery/entities")).status_code == 401
            client.headers["Authorization"] = "Bearer reader"
            assert (await client.get("/v1/discovery/entities")).json() == {"items": []}
            assert (await client.get("/v1/discovery/entities?limit=101")).status_code == 422
            assert (await client.get("/v1/articles?topic=前瞻")).status_code == 200
            assert "discovery" in (await client.get("/v1/status")).json()
            assert (await client.post("/v1/admin/jobs?kind=discover")).status_code == 403
            client.headers["Authorization"] = "Bearer admin"
            imported = await client.post("/v1/admin/import", json={"articles": [item().model_dump(mode="json")]})
            assert imported.status_code == 200 and imported.json()["accepted"] == 0
            jobs = (await client.get("/v1/status")).json()["jobs"]
            assert len(jobs) == 1 and jobs[0]["kind"] == "discover" and jobs[0]["status"] == "queued"
            response = await client.post("/v1/admin/jobs?kind=discover")
            assert response.status_code == 202 and response.json()["job_id"] == jobs[0]["id"]
    finally:
        await supervisor.stop()


def test_cli_accepts_discover_through_normal_pipeline_entry(monkeypatch):
    captured = []
    monkeypatch.setattr("sys.argv", ["radar", "discover"])
    monkeypatch.setattr(cli, "_run", lambda args, parser, rechecking: captured.append((args.action, rechecking)))
    cli.main()
    assert captured == [("discover", False)]


def seed_trial(sessions, *, expired=False):
    with sessions.begin() as session:
        session.add(Watch(id="x:trialaccount", handle="trialaccount", name="Trial account", enabled=True))
        session.add(DiscoveryEntity(id="x:123", name="Trial account", handle="trialaccount", status="trial"))
        session.flush()
        session.add(DiscoveryWatch(watch_id="x:trialaccount", entity_id="x:123", status="trial", managed=True,
                                   expires_at=(datetime.now(UTC) + timedelta(days=-1 if expired else 1)).isoformat()))


async def test_collect_expires_trial_before_resolving_watched_handles(env, monkeypatch):
    pipeline, model = env
    seed_trial(pipeline.sessions, expired=True)
    observed = []

    async def collect(self, client, handles, ingest_page):
        observed.extend(handles)
        return XCollectionResult(status="healthy")

    async def facebook(*args):
        return []

    monkeypatch.setattr("radar.pipeline.XCollector.collect", collect)
    monkeypatch.setattr("radar.pipeline.fetch_facebook", facebook)
    assert await pipeline.collect() == 0
    assert "trialaccount" not in observed and model.calls == []
    with pipeline.sessions() as session:
        assert not session.get(Watch, "x:trialaccount").enabled
        assert session.get(DiscoveryWatch, "x:trialaccount").status == "expired"


async def test_watch_api_exposes_trial_metadata_and_user_toggle_takes_control(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('anthropic_news_enabled=false\n[provider]\nkind="extractive"\n')
    app = api.create_app(Settings(config_path=str(config), database_url=f"sqlite:///{tmp_path}/watch-api.db",
                                  reader_token="reader", admin_token="admin", scheduler_enabled=False))
    seed_trial(app.state.sessions)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture",
                                     headers={"Authorization": "Bearer reader"}) as client:
            watches = (await client.get("/v1/watches")).json()["items"]
            trial = next(row for row in watches if row["id"] == "x:trialaccount")
            assert trial["discovery"]["origin"] == "automatic" and trial["discovery"]["status"] == "trial"
            assert any("discovery" not in row for row in watches if row["id"] != trial["id"])
            assert (await client.patch("/v1/watches/x:trialaccount", json={"enabled": False})).status_code == 200
            with app.state.sessions() as session:
                saved = session.get(DiscoveryWatch, "x:trialaccount")
                assert saved.status == "user_stopped" and not saved.managed
                assert not session.get(Watch, "x:trialaccount").enabled
            assert (await client.patch("/v1/watches/x:trialaccount", json={"enabled": True})).status_code == 200
            with app.state.sessions() as session:
                assert session.get(DiscoveryWatch, "x:trialaccount").status == "retained"
                assert session.get(Watch, "x:trialaccount").enabled
    finally:
        await app.state.supervisor.stop()

"""Reader access, candidate isolation, refresh coalescing and lane separation."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar.api import create_app
from radar.config import Settings
from radar.industry import put_evidence
from radar.industry_models import IndustryAssessment, IndustryEvidence
from radar.industry_sources import EvidenceInput
from radar.models import Job


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('anthropic_news_enabled=false\n[provider]\nkind="extractive"\n'
                    '[industry]\nenabled=true\nanalysis_enabled=false\n')
    value = create_app(Settings(config_path=str(path), database_url=f"sqlite:///{tmp_path}/api.db",
                                reader_token="fixture-reader", admin_token="fixture-admin",
                                scheduler_enabled=False, accounts_config_path="", usage_database_path=""))
    # No lifespan is started: refresh tests inspect durable queue intent without
    # executing source collection, other jobs, or real model calls.
    yield value
    with value.state.sessions() as session:
        session.bind.dispose()


def headers():
    return {"Authorization": "Bearer fixture-reader"}


def add_evidence(app, *, text="GPU订单已披露，尚待经营验证。"):
    now = datetime.now(UTC)
    with app.state.pipeline.industry.transaction() as session:
        put_evidence(session, EvidenceInput(source_id="nvidia-news", external_id="fixture",
            url="https://nvidianews.nvidia.com/news/fixture", title="GPU revenue",
            text=text, published_at=(now - timedelta(hours=1)).isoformat(), published_precision="timestamp",
            kind="company_release", entity_ids=["nvidia"], metadata={"partial": False}), "NVIDIA", clock=now)
        return session.scalar(select(IndustryEvidence).where(IndustryEvidence.current.is_(True))).id


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/v1/industry", None), ("GET", "/v1/industry/evidence", None),
    ("GET", "/v1/industry/evidence/test", None), ("GET", "/v1/industry/jobs/test", None),
    ("POST", "/v1/industry/refresh", None),
    ("PUT", "/v1/industry/themes/infrastructure/tracking", {"enabled": False}),
])
def test_industry_routes_require_reader_auth(app, method, path, payload):
    response = TestClient(app).request(method, path, json=payload)
    assert response.status_code == 401


def test_reader_can_read_versions_and_candidates_are_not_exposed(app):
    uid = add_evidence(app)
    now = datetime.now(UTC)
    with app.state.sessions.begin() as session:
        session.add(IndustryAssessment(id="unapproved", theme_id="infrastructure", fingerprint="fixture",
            as_of=now.isoformat(), created_at=now.isoformat(), status="pending", stage="audit",
            candidate={"summary_zh": "SECRET-UNAPPROVED-CANDIDATE"},
            history=[{"event": "response", "text": "SECRET-RAW-MODEL-RESPONSE"}]))
    client = TestClient(app)
    response = client.get("/v1/industry", headers=headers())
    assert response.status_code == 200
    assert "SECRET" not in response.text
    result = next(row for row in response.json()["themes"] if row["id"] == "infrastructure")
    assert result["assessment"] is None and result["analysis_status"]["status"] == "pending"
    assert client.get(f"/v1/industry/evidence/{uid}", headers=headers()).json()["id"] == uid
    assert client.get("/v1/industry/evidence/missing", headers=headers()).status_code == 404


def test_reader_evidence_filters_are_bounded_and_literal(app):
    add_evidence(app)
    client = TestClient(app)
    assert client.get("/v1/industry/evidence?theme=missing", headers=headers()).status_code == 404
    for query in ("limit=0", "limit=101", "offset=-1", "offset=10001", "q=" + "x" * 201):
        assert client.get("/v1/industry/evidence?" + query, headers=headers()).status_code == 422
    assert client.get("/v1/industry/evidence?q=%25", headers=headers()).json()["total"] == 0
    assert client.get("/v1/industry/evidence?theme=infrastructure&q=GPU", headers=headers()).json()["total"] == 1


def test_refresh_coalesces_without_forcing_analysis_or_other_collection(app):
    client = TestClient(app)
    first = client.post("/v1/industry/refresh", headers=headers())
    second = client.post("/v1/industry/refresh", headers=headers())
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["kind"] == "industry_collect"
    with app.state.sessions() as session:
        jobs = session.scalars(select(Job)).all()
        assert len(jobs) == 1 and jobs[0].kind == "industry_collect" and not jobs[0].force
        assert not session.scalars(select(IndustryAssessment)).all()


def test_tracking_pause_and_disabled_service_block_refresh(app):
    client = TestClient(app)
    assert client.put("/v1/industry/themes/missing/tracking", json={"enabled": False},
                      headers=headers()).status_code == 404
    for theme in ("infrastructure", "software", "workflows"):
        response = client.put(f"/v1/industry/themes/{theme}/tracking", json={"enabled": False}, headers=headers())
        assert response.status_code == 200 and not response.json()["enabled"]
    assert client.post("/v1/industry/refresh", headers=headers()).status_code == 409
    app.state.pipeline.industry.options.enabled = False
    assert client.post("/v1/industry/refresh", headers=headers()).status_code == 409
    assert client.put("/v1/industry/themes/software/tracking", json={"enabled": True},
                      headers=headers()).status_code == 409


def test_job_status_cannot_read_other_job_namespaces_or_internal_owner(app):
    with app.state.sessions.begin() as session:
        session.add(Job(id="ordinary", kind="collect", status="queued", owner="private-owner"))
        session.add(Job(id="industry", kind="industry_analyze", status="queued", owner="private-owner"))
    client = TestClient(app)
    assert client.get("/v1/industry/jobs/ordinary", headers=headers()).status_code == 404
    assert client.get("/v1/industry/jobs/missing", headers=headers()).status_code == 404
    response = client.get("/v1/industry/jobs/industry", headers=headers())
    assert response.status_code == 200 and "private-owner" not in response.text


@pytest.mark.parametrize("kind", ["industry_collect", "industry_analyze"])
async def test_managed_industry_lanes_do_not_run_or_block_existing_news_work(app, monkeypatch, kind):
    pipeline = app.state.pipeline
    calls, phases = [], []

    async def forbidden(*args, **kwargs):
        pytest.fail("Industry lane must not invoke existing article/model pipelines")

    async def collect():
        calls.append("industry_collect")
        return {"checked": 1, "added": 1, "failed": 0}

    async def analyze():
        calls.append("industry_analyze")
        return {"processed": 1, "status": "pending"}

    async def phase(value):
        phases.append(value)

    monkeypatch.setattr(pipeline, "collect", forbidden)
    monkeypatch.setattr(pipeline, "digest", forbidden)
    monkeypatch.setattr(pipeline.translations, "pending", forbidden)
    monkeypatch.setattr(pipeline.reading, "pending", forbidden)
    monkeypatch.setattr(pipeline.presentations, "pending", forbidden)
    monkeypatch.setattr(pipeline.discovery, "pending", forbidden)
    monkeypatch.setattr(pipeline.industry, "collect", collect)
    monkeypatch.setattr(pipeline.industry, "analyze", analyze)
    monkeypatch.setattr(pipeline.industry, "due", lambda: False)
    monkeypatch.setattr(pipeline.industry, "has_pending", lambda: kind == "industry_analyze")
    with pipeline.sessions.begin() as session:
        job = Job(kind=kind, status="running", owner="fixture-owner", queued_at=datetime.now(UTC).isoformat())
        session.add(job)
        session.flush()
        uid = job.id
    async with pipeline.lock, pipeline.collect_lock, pipeline.translate_lock, pipeline.presentation_lock:
        assert await asyncio.wait_for(pipeline.run(kind=kind, force=True, job_id=uid, phase_callback=phase), 1) == uid
    assert calls == [kind] and phases == [kind]
    with pipeline.sessions() as session:
        job = session.get(Job, uid)
        assert job.status == "completed"
        assert job.more_pending == (kind == "industry_analyze")

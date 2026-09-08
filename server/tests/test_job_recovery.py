"""Isolated queue/lifecycle regressions; no collector, network or model is called."""

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from radar import api, jobs
from radar.config import RadarConfig, Settings
from radar.daily_schedule import DailySchedule
from radar.db import JOB_CONTROL_COLUMNS, database
from radar.jobs import JobQueueConflict, JobSupervisor, public_job
from radar.models import Job


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 8, 2, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakePipeline:
    def __init__(self, sessions):
        self.sessions, self.config = sessions, RadarConfig()
        self.calls = []
        self.lock = asyncio.Lock()
        self.pending = set()
        self.action = None

    def has_pending(self, kind):
        return kind in self.pending

    async def run(self, **kwargs):
        self.calls.append({key: value for key, value in kwargs.items() if key != "phase_callback"})
        await kwargs["phase_callback"]("collect" if kwargs["kind"] == "daily" else kwargs["kind"])
        if self.action:
            await self.action(**kwargs)
        return kwargs["job_id"]


@pytest.fixture
def store(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/queue.db")
    yield sessions
    engine.dispose()


@pytest.fixture
async def supervisor(store):
    service = JobSupervisor(FakePipeline(store), automatic=False, poll_seconds=0.005,
                            heartbeat_seconds=0.01)
    yield service
    await service.stop()


async def until(predicate, *, timeout=1):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout)


def row(store, uid):
    with store() as session:
        return session.get(Job, uid)


def rows(store):
    with store() as session:
        return session.scalars(select(Job).order_by(Job.queued_at, Job.id)).all()


async def test_queued_job_is_durable_and_not_reported_as_running(supervisor, store):
    supervisor.stopping = True
    uid = supervisor.submit("translate", force=True)
    waiting = row(store, uid)
    assert waiting.status == "queued" and waiting.attempt == 0
    assert public_job(waiting)["started_at"] is None
    assert waiting.queued_at and waiting.heartbeat_at is None
    assert "owner" not in public_job(waiting) and "force" not in public_job(waiting)
    await supervisor.start()
    await until(lambda: row(store, uid).status == "completed")
    assert public_job(row(store, uid))["started_at"]
    assert supervisor.pipeline.calls[0]["force"] is True


async def test_repeated_inputs_coalesce_one_queued_followup_without_losing_force(supervisor, store):
    started, release = asyncio.Event(), asyncio.Event()

    async def hold(**kwargs):
        if len(supervisor.pipeline.calls) == 1:
            started.set()
            await release.wait()

    supervisor.pipeline.action = hold
    first = supervisor.submit("read")
    await asyncio.wait_for(started.wait(), 1)
    followups = [supervisor.submit("read", force=n == 12) for n in range(30)]
    assert len(set(followups)) == 1 and followups[0] != first
    assert sorted(item.status for item in rows(store)) == ["queued", "running"]
    release.set()
    await until(lambda: all(item.status == "completed" for item in rows(store)))
    assert len(supervisor.pipeline.calls) == 2
    assert supervisor.pipeline.calls[1]["force"] is True


async def test_collect_and_long_read_do_not_block_translation_lane(supervisor, store):
    entered = {kind: asyncio.Event() for kind in ("collect", "read", "translate")}
    release = asyncio.Event()

    async def hold(**kwargs):
        entered[kwargs["kind"]].set()
        await release.wait()

    supervisor.pipeline.action = hold
    for kind in entered:
        supervisor.submit(kind)
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 1)
    assert len([item for item in rows(store) if item.status == "running"]) == 3
    release.set()


async def test_processing_kinds_rotate_instead_of_read_continuation_starvation(supervisor, store):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(**kwargs):
        if len(supervisor.pipeline.calls) == 1:
            entered.set()
            await release.wait()

    supervisor.pipeline.action = hold
    supervisor.submit("read")
    await asyncio.wait_for(entered.wait(), 1)
    supervisor.submit("read")  # Older continuation than the digest request.
    supervisor.submit("digest", day=date(2026, 9, 8))
    release.set()
    await until(lambda: len(supervisor.pipeline.calls) == 3)
    assert [call["kind"] for call in supervisor.pipeline.calls] == ["read", "digest", "read"]


async def test_heartbeat_is_liveness_not_fabricated_stage_progress(supervisor, store):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(**kwargs):
        entered.set()
        await release.wait()

    supervisor.pipeline.action = hold
    uid = supervisor.submit("translate")
    await asyncio.wait_for(entered.wait(), 1)
    before = row(store, uid)
    await until(lambda: row(store, uid).heartbeat_at != before.heartbeat_at)
    after = row(store, uid)
    assert after.phase == "translate" and after.progress_at == before.progress_at
    release.set()


async def test_shutdown_persists_intent_and_restart_keeps_request_parameters(supervisor, store):
    entered = asyncio.Event()

    async def hold(**kwargs):
        entered.set()
        await asyncio.Future()

    supervisor.pipeline.action = hold
    uid = supervisor.submit("read", day=date(2026, 9, 7), force=True)
    await asyncio.wait_for(entered.wait(), 1)
    await supervisor.stop()
    stopped = row(store, uid)
    assert stopped.status == "queued" and stopped.reason_code == "cancelled"
    assert stopped.owner == "" and stopped.attempt == 0
    restarted = JobSupervisor(FakePipeline(store), automatic=False, poll_seconds=0.005)
    try:
        await restarted.start()
        await until(lambda: row(store, uid).status == "completed")
        call = restarted.pipeline.calls[0]
        assert call["day"] == date(2026, 9, 7) and call["force"] is True
        assert row(store, uid).attempt == 1
    finally:
        await restarted.stop()


async def test_task_construction_failure_does_not_erase_committed_intent(supervisor, store, monkeypatch):
    def unavailable(coro, **kwargs):
        raise RuntimeError("synthetic task factory failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(jobs.asyncio, "create_task", unavailable)
        with pytest.raises(RuntimeError, match="task factory"):
            supervisor.submit("translate")
    pending = rows(store)
    assert len(pending) == 1 and pending[0].status == "queued"
    assert pending[0].attempt == 0 and supervisor.pipeline.calls == []
    await supervisor.start()
    await until(lambda: row(store, pending[0].id).status == "completed")
    assert len(supervisor.pipeline.calls) == 1


async def test_known_transport_failure_has_persistent_backoff_and_three_attempt_cap(store):
    clock = Clock()
    pipeline = FakePipeline(store)

    async def fail(**kwargs):
        raise TimeoutError("synthetic secret must not be exposed")

    pipeline.action = fail
    service = JobSupervisor(pipeline, clock=clock, automatic=False, poll_seconds=0.005)
    try:
        uid = service.submit("translate")
        await until(lambda: row(store, uid).status == "retrying")
        assert row(store, uid).retry_at == (clock() + timedelta(seconds=60)).isoformat()
        await asyncio.sleep(0.02)
        assert len(pipeline.calls) == 1
        clock.advance(60)
        await until(lambda: row(store, uid).attempt == 2 and row(store, uid).status == "retrying")
        clock.advance(300)
        await until(lambda: row(store, uid).status == "needs_attention")
        assert row(store, uid).attempt == 3
        assert "secret" not in row(store, uid).message
        pipeline.pending.add("translate")
        for _ in range(10):
            service._enqueue_eligible()
        assert len(rows(store)) == 1 and len(pipeline.calls) == 3
    finally:
        await service.stop()


async def test_unknown_exception_is_attention_not_automatic_model_retry(supervisor, store):
    async def fail(**kwargs):
        raise RuntimeError("synthetic unknown model outcome")

    supervisor.pipeline.action = fail
    uid = supervisor.submit("read")
    await until(lambda: row(store, uid).status == "needs_attention")
    assert row(store, uid).attempt == 1 and row(store, uid).reason_code == "task_error"
    supervisor.pipeline.pending.add("read")
    for _ in range(10):
        supervisor._enqueue_eligible()
    assert len(rows(store)) == 1 and len(supervisor.pipeline.calls) == 1


async def test_periodic_eligibility_does_not_enqueue_noop_or_duplicate_running_work(supervisor, store):
    supervisor.stopping = True
    supervisor._enqueue_eligible()
    assert rows(store) == []
    supervisor.pipeline.pending.add("translate")
    for _ in range(20):
        supervisor._enqueue_eligible()
    assert len(rows(store)) == 1 and rows(store)[0].kind == "translate"


def test_discovery_supervisor_failure_rechecks_durable_queue_after_backoff(store):
    clock = Clock()
    pipeline = FakePipeline(store)
    pipeline.config.discovery.session_reuse = True
    pipeline.pending.add("discover")
    service = JobSupervisor(pipeline, clock=clock, automatic=False)
    service.stopping = True
    with store.begin() as session:
        session.add(Job(id="interrupted-batch", kind="discover", status="needs_attention",
            reason_code="task_error", finished_at=clock().isoformat(), queued_at=clock().isoformat()))
    service._enqueue_eligible()
    assert len(rows(store)) == 1
    clock.advance(899)
    service._enqueue_eligible()
    assert len(rows(store)) == 1
    clock.advance(1)
    service._enqueue_eligible()
    service._enqueue_eligible()
    assert len(rows(store)) == 2
    assert row(store, "interrupted-batch").status == "needs_attention"
    assert sum(job.status == "queued" for job in rows(store)) == 1


async def test_conflicting_digest_dates_are_not_silently_overwritten(supervisor, store):
    supervisor.stopping = True
    uid = supervisor.submit("digest", day=date(2026, 9, 7))
    with pytest.raises(JobQueueConflict):
        supervisor.submit("digest", day=date(2026, 9, 8))
    assert row(store, uid).request_day == "2026-09-07"


async def test_legacy_unproven_job_blocks_model_lane_but_not_new_collection(supervisor, store):
    with store.begin() as session:
        session.add(Job(id="legacy", kind="translate", status="running"))
    translate = supervisor.submit("translate")
    collect = supervisor.submit("collect")
    await until(lambda: row(store, collect).status == "completed")
    assert row(store, translate).status == "queued"
    assert row(store, "legacy").status == "running"
    assert [call["kind"] for call in supervisor.pipeline.calls] == ["collect"]


async def test_explicit_legacy_handoff_does_not_guess_missing_digest_date(supervisor, store):
    with store.begin() as session:
        session.add(Job(id="old-digest", kind="digest", status="running"))
        session.add(Job(id="old-read", kind="read", status="running"))
    await supervisor.start(recover_legacy=True)
    await until(lambda: row(store, "old-read").status == "completed")
    assert row(store, "old-digest").status == "needs_attention"
    assert [call["kind"] for call in supervisor.pipeline.calls] == ["read"]
    assert supervisor.pipeline.calls[0]["force"] is False


async def test_process_lock_prevents_second_api_worker_from_recovering_live_owner(supervisor, store):
    await supervisor.start()
    second = JobSupervisor(FakePipeline(store), automatic=False)
    try:
        with pytest.raises(RuntimeError, match="另一个"):
            await second.start(recover_legacy=True)
    finally:
        await second.stop()


async def test_daily_window_budget_does_not_multiply_with_transport_retries(supervisor, store):
    clock = Clock()
    supervisor.clock = clock
    schedule = DailySchedule(supervisor.pipeline, clock=clock, submit=supervisor.submit)

    async def fail(**kwargs):
        raise TimeoutError("fixture transport")

    supervisor.pipeline.action = fail
    first = await schedule.run()
    await until(lambda: row(store, first).status == "needs_attention")
    assert row(store, first).max_attempts == 1
    for _ in range(10):
        assert await schedule.run() is None
    clock.advance(1800)
    second = await schedule.run()
    await until(lambda: row(store, second).status == "needs_attention")
    clock.advance(7200)
    third = await schedule.run()
    await until(lambda: row(store, third).status == "needs_attention")
    clock.advance(3600)
    assert await schedule.run() is None
    assert len(supervisor.pipeline.calls) == 3


def test_job_migration_is_idempotent_and_preserves_old_columns_and_data(tmp_path):
    path = tmp_path / "legacy.db"
    before = ("old", "read", "running", "2026-09-08T00:00:00+00:00", None, "original receipt")
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY, kind VARCHAR(30) NOT NULL, "
                           "status VARCHAR(30) NOT NULL, started_at VARCHAR(40) NOT NULL, "
                           "finished_at VARCHAR(40), message TEXT NOT NULL)")
        connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", before)
    for _ in range(2):
        engine, _ = database(f"sqlite:///{path}")
        engine.dispose()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT id,kind,status,started_at,finished_at,message FROM jobs",
        ).fetchone() == before
        columns = {item[1] for item in connection.execute("PRAGMA table_info(jobs)")}
    assert set(JOB_CONTROL_COLUMNS) <= columns


async def test_import_while_busy_keeps_durable_followup_and_status_is_safe(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('anthropic_news_enabled=false\n[provider]\nkind="extractive"\n'
                      '[translation]\nenabled=true\n[reading]\nenabled=true\n')
    app = api.create_app(Settings(config_path=str(config), database_url=f"sqlite:///{tmp_path}/api.db",
                                  reader_token="reader", admin_token="admin", scheduler_enabled=False))
    service = app.state.supervisor
    # Pause execution only, not enqueue: import must durably record both followups.
    service.stopping = True
    transport = httpx.ASGITransport(app=app)
    body = {"articles": [{"platform": "x", "external_id": "fixture1",
                         "url": "https://x.com/example/status/100", "title": "AI model research",
                         "text": "Researchers published an AI model.", "author": "Example",
                         "handle": "example", "published_at": datetime.now(UTC).isoformat(),
                         "metrics": {"like_count": 200}}]}
    try:
        async with app.state.pipeline.lock, httpx.AsyncClient(transport=transport, base_url="http://fixture",
                headers={"Authorization": "Bearer admin"}) as client:
            response = await client.post("/v1/admin/import", json=body)
            assert response.status_code == 200 and response.json()["accepted"] == 1
            state = (await client.get("/v1/status")).json()
            assert state["job_counts"] == {"queued": 2}
            assert {job["kind"] for job in state["jobs"]} == {"read", "translate"}
            assert all(job["started_at"] is None for job in state["jobs"])
            assert all("owner" not in job and "request_day" not in job for job in state["jobs"])
            assert state["server_now"]
    finally:
        await service.stop()


async def test_dead_managed_owner_recovers_but_unknown_owner_does_not(supervisor, store, monkeypatch):
    now = datetime.now(UTC).isoformat()
    with store.begin() as session:
        session.add(Job(id="dead", kind="read", status="running", queued_at=now,
                        owner="api:123456:dead", request_day="2026-09-06", force=True, attempt=1))
        session.add(Job(id="uncertain", kind="translate", status="running", queued_at=now,
                        owner="unrecognized-owner", attempt=1))

    def absent(pid, signal):
        assert pid == 123456
        raise ProcessLookupError

    monkeypatch.setattr(jobs.os, "kill", absent)
    await supervisor.start()
    await until(lambda: row(store, "dead").status == "completed")
    assert row(store, "dead").attempt == 1
    assert row(store, "uncertain").status == "running"
    assert supervisor.pipeline.calls[0]["day"] == date(2026, 9, 6)
    assert supervisor.pipeline.calls[0]["force"] is True


async def test_stale_worker_cannot_update_phase_or_finish_reassigned_job(supervisor, store):
    supervisor.stopping = True
    uid = supervisor.submit("read")
    with store.begin() as session:
        job = session.get(Job, uid)
        job.status, job.owner, job.phase = "running", "another-owner", "read"
    with pytest.raises(RuntimeError, match="执行权"):
        await supervisor._phase(uid, "digest")
    assert supervisor._finish(uid) is None
    supervisor._failure(uid, TimeoutError())
    current = row(store, uid)
    assert (current.owner, current.status, current.phase) == ("another-owner", "running", "read")


async def test_completed_stage_continuation_waits_without_restarting_finished_job(store):
    clock = Clock()
    pipeline = FakePipeline(store)

    async def stage(**kwargs):
        if len(pipeline.calls) == 1:
            with store.begin() as session:
                session.get(Job, kwargs["job_id"]).more_pending = True

    pipeline.action = stage
    service = JobSupervisor(pipeline, clock=clock, automatic=False, poll_seconds=0.005)
    try:
        first = service.submit("translate", force=True)
        await until(lambda: len(rows(store)) == 2)
        waiting = next(item for item in rows(store) if item.id != first)
        assert row(store, first).status == "completed" and waiting.status == "queued"
        assert waiting.retry_at == (clock() + timedelta(seconds=30)).isoformat()
        assert len(pipeline.calls) == 1
        clock.advance(30)
        await until(lambda: row(store, waiting.id).status == "completed")
        assert len(pipeline.calls) == 2 and pipeline.calls[1]["force"] is False
    finally:
        await service.stop()


async def test_daily_intent_retains_date_if_submit_fails_before_worker_creation(supervisor, store):
    def unavailable(**kwargs):
        raise RuntimeError("synthetic process died before enqueue")

    schedule = DailySchedule(supervisor.pipeline, clock=Clock(), submit=unavailable)
    with pytest.raises(RuntimeError, match="before enqueue"):
        await schedule.run()
    pending = rows(store)
    assert len(pending) == 1 and pending[0].status == "queued"
    assert pending[0].request_day == "2026-09-08" and pending[0].max_attempts == 1
    await supervisor.start()
    await until(lambda: row(store, pending[0].id).status == "completed")
    assert supervisor.pipeline.calls[0]["day"] == date(2026, 9, 8)


async def test_verified_legacy_backlog_coalesces_eleven_reads_without_deleting_history(supervisor, store):
    with store.begin() as session:
        for index in range(11):
            session.add(Job(id=f"legacy-{index:02}", kind="read", status="running"))
    await supervisor.start(recover_legacy=True)
    await until(lambda: all(item.status == "completed" for item in rows(store)))
    history = rows(store)
    assert len(history) == 11 and len(supervisor.pipeline.calls) == 1
    assert len([item for item in history if item.reason_code == "coalesced"]) == 10
    assert len([item for item in history if item.queued_at]) == 1
    assert supervisor.pipeline.calls[0]["force"] is False


async def test_repeated_shutdowns_do_not_exhaust_transport_budget(store):
    uid = None
    for _ in range(4):
        pipeline = FakePipeline(store)
        entered = asyncio.Event()

        async def hold(entered=entered, **kwargs):
            entered.set()
            await asyncio.Future()

        pipeline.action = hold
        service = JobSupervisor(pipeline, automatic=False, poll_seconds=0.005)
        try:
            if uid is None:
                uid = service.submit("translate")
            else:
                await service.start()
            await asyncio.wait_for(entered.wait(), 1)
        finally:
            await service.stop()
        assert row(store, uid).status == "queued" and row(store, uid).attempt == 0
    resumed = JobSupervisor(FakePipeline(store), automatic=False, poll_seconds=0.005)
    try:
        await resumed.start()
        await until(lambda: row(store, uid).status == "completed")
        assert row(store, uid).attempt == 1
    finally:
        await resumed.stop()


async def test_cancellation_does_not_erase_a_previous_known_transport_failure(store):
    clock, pipeline = Clock(), FakePipeline(store)
    entered = asyncio.Event()

    async def action(**kwargs):
        if len(pipeline.calls) == 1:
            raise TimeoutError("known terminated timeout")
        entered.set()
        await asyncio.Future()

    pipeline.action = action
    service = JobSupervisor(pipeline, clock=clock, automatic=False, poll_seconds=0.005)
    uid = service.submit("translate")
    try:
        await until(lambda: row(store, uid).status == "retrying")
        clock.advance(60)
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        await service.stop()
    assert row(store, uid).attempt == 1 and row(store, uid).status == "queued"


async def test_due_daily_defers_to_queued_explicit_older_date(supervisor, store):
    supervisor.stopping = True
    uid = supervisor.submit("digest", day=date(2026, 9, 7))
    schedule = DailySchedule(supervisor.pipeline, clock=Clock(), submit=supervisor.submit)
    assert await schedule.run() is None
    assert len(rows(store)) == 1 and row(store, uid).request_day == "2026-09-07"


async def test_offline_recovery_only_restores_intent_without_starting_workers(supervisor, store):
    with store.begin() as session:
        for index in range(11):
            session.add(Job(id=f"offline-{index:02}", kind="read", status="running"))
    supervisor.pipeline.pending.update(("translate", "read"))
    supervisor.automatic = True
    supervisor.recover_interrupted(recover_legacy=True)
    assert supervisor.workers == {} and supervisor.maintenance is None
    assert supervisor.pipeline.calls == []
    state = rows(store)
    assert len(state) == 11
    assert len([item for item in state if item.status == "queued"]) == 1
    assert len([item for item in state if item.reason_code == "coalesced"]) == 10
    await supervisor.stop()
    assert supervisor.lock_file is None


async def test_dead_managed_job_merges_followup_without_losing_force_or_retry_budget(supervisor, store, monkeypatch):
    with store.begin() as session:
        session.add(Job(id="interrupted", kind="read", status="running", owner="api:123456:dead",
                        queued_at="2026-09-08T00:00:00+00:00", request_day="2026-09-07", attempt=2))
        session.add(Job(id="followup", kind="read", status="queued", force=True,
                        queued_at="2026-09-08T00:01:00+00:00", request_day="2026-09-07", attempt=0))
        session.add(Job(id="different-date", kind="read", status="queued",
                        queued_at="2026-09-08T00:02:00+00:00", request_day="2026-09-06", attempt=0))

    def absent(pid, signal):
        assert pid == 123456
        raise ProcessLookupError

    monkeypatch.setattr(jobs.os, "kill", absent)
    supervisor.recover_interrupted()
    assert supervisor.pipeline.calls == [] and supervisor.workers == {}
    current = row(store, "interrupted")
    assert current.status == "queued" and current.force is True and current.attempt == 1
    assert row(store, "followup").status == "completed"
    assert row(store, "followup").reason_code == "coalesced"
    assert row(store, "different-date").status == "queued"
    # Repeating read-only owner reconciliation does not create extra work or clear failures.
    supervisor.recover_interrupted()
    assert len(rows(store)) == 3 and row(store, "interrupted").attempt == 1

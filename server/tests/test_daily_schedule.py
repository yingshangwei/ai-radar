import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radar import api
from radar import pipeline as pipeline_module
from radar.config import ProviderConfig, RadarConfig, ReadingConfig, Settings, TranslationConfig
from radar.daily_schedule import DailySchedule
from radar.db import database
from radar.models import Digest, Job
from radar.pipeline import Pipeline, digest_window


class Clock:
    def __init__(self, value="2020-09-08T02:00:00+00:00"):
        self.value = datetime.fromisoformat(value)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


@pytest.fixture
def store(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path}/schedule.db")
    yield sessions
    engine.dispose()


def publish(session, day, config, source_count=1):
    start, end = digest_window(day, config)
    session.add(Digest(
        date=day.isoformat(), title="Fixture", overview="Fixture", stories=[],
        provider="fixture" if source_count else "no_updates", source_count=source_count,
        window_start=start.isoformat(), window_end=end.isoformat(), coverage=[],
    ))


def harness(store, monkeypatch, *, clock=None, config=None, failures=0):
    clock = clock or Clock()
    config = config or RadarConfig(
        provider=ProviderConfig(kind="extractive"),
        translation=TranslationConfig(enabled=False),
        reading=ReadingConfig(enabled=False), enrich_official_articles=False,
    )
    monkeypatch.setattr(pipeline_module, "now_iso", lambda: clock().isoformat())
    state = {"collects": 0, "digests": [], "failures": failures}

    def build():
        pipeline = Pipeline(store, config)

        async def collect():
            state["collects"] += 1
            return 0

        async def digest(day, force=False):
            assert force is False
            state["digests"].append(day)
            if state["failures"]:
                state["failures"] -= 1
                raise RuntimeError("fixture model unavailable")
            with store.begin() as session:
                publish(session, day, config)
            return day.isoformat()

        pipeline.collect = collect
        pipeline.digest = digest
        return pipeline, DailySchedule(pipeline, clock=clock)

    pipeline, schedule = build()
    return clock, state, pipeline, schedule, build


@pytest.mark.parametrize(("zone", "hour", "instant", "expected"), [
    ("Asia/Shanghai", 8, "2020-09-08T00:00:00+00:00", "2020-09-08"),
    ("Asia/Shanghai", 8, "2020-09-07T23:59:59+00:00", "2020-09-07"),
    ("Pacific/Kiritimati", 8, "2020-09-07T18:00:00+00:00", "2020-09-08"),
    ("America/New_York", 2, "2026-03-08T06:59:00+00:00", "2026-03-07"),
    ("America/New_York", 2, "2026-03-08T07:00:00+00:00", "2026-03-08"),
    ("America/New_York", 1, "2026-11-01T06:30:00+00:00", "2026-11-01"),
])
def test_latest_due_window_uses_configured_timezone_and_dst(store, zone, hour, instant, expected):
    config = RadarConfig(timezone=zone, daily_hour=hour)
    schedule = DailySchedule(Pipeline(store, config))
    now = datetime.fromisoformat(instant)
    day, start, end = schedule.due_window(now)
    assert day == date.fromisoformat(expected)
    assert (start, end) == digest_window(day, config)
    assert end <= now


@pytest.mark.asyncio
async def test_missed_startup_generates_only_latest_window_once(store, monkeypatch):
    _, state, _, schedule, rebuild = harness(store, monkeypatch)
    uid = await schedule.run()
    assert uid is not None
    assert state["digests"] == [date(2020, 9, 8)]
    assert state["collects"] == 1
    for _ in range(20):
        assert await schedule.run() is None
    _, restarted = rebuild()
    assert await restarted.run() is None
    with store() as session:
        assert len(session.scalars(select(Job)).all()) == 1
        assert session.get(Job, uid).status == "completed"
        assert len(session.scalars(select(Digest)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [0, 4])
async def test_manual_or_no_updates_edition_is_never_automatically_rewritten(store, monkeypatch, source_count):
    _, state, pipeline, schedule, _ = harness(store, monkeypatch)
    with store.begin() as session:
        publish(session, date(2020, 9, 8), pipeline.config, source_count)
    assert await schedule.run() is None
    assert state["digests"] == []
    assert state["collects"] == 0
    with store() as session:
        assert session.scalar(select(Job)) is None
        assert session.get(Digest, "2020-09-08").source_count == source_count


@pytest.mark.asyncio
async def test_failures_back_off_and_stop_after_three_attempts_across_restart(store, monkeypatch):
    clock, state, _, schedule, rebuild = harness(store, monkeypatch, failures=10)
    first = await schedule.run()
    for _ in range(10):
        assert await schedule.run() is None
    clock.advance(minutes=29, seconds=59)
    assert await schedule.run() is None
    clock.advance(seconds=1)
    _, schedule = rebuild()
    second = await schedule.run()
    assert second and second != first
    clock.advance(hours=1, minutes=59, seconds=59)
    assert await schedule.run() is None
    clock.advance(seconds=1)
    _, schedule = rebuild()
    third = await schedule.run()
    assert third not in (None, first, second)
    clock.advance(hours=8)
    _, schedule = rebuild()
    assert await schedule.run() is None
    assert len(state["digests"]) == 3
    assert state["collects"] == 1  # Correction attempts use digest, not another X collection.
    with store() as session:
        jobs = session.scalars(select(Job).order_by(Job.started_at)).all()
        assert [job.kind for job in jobs] == ["daily", "digest", "digest"]
        assert all(job.status == "failed" for job in jobs)
        assert session.scalar(select(Digest)) is None


@pytest.mark.asyncio
async def test_successful_retry_stops_future_model_attempts(store, monkeypatch):
    clock, state, _, schedule, _ = harness(store, monkeypatch, failures=1)
    await schedule.run()
    clock.advance(minutes=30)
    second = await schedule.run()
    clock.advance(hours=8)
    assert await schedule.run() is None
    assert len(state["digests"]) == 2
    with store() as session:
        assert session.get(Job, second).status == "completed"
        assert session.get(Digest, "2020-09-08") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["daily", "digest"])
async def test_legacy_api_failure_counts_toward_retry_cap_and_backoff(store, monkeypatch, kind):
    clock, state, _, schedule, _ = harness(store, monkeypatch, failures=10)
    with store.begin() as session:
        session.add(Job(
            id="legacy-api-job", kind=kind, status="failed", started_at=clock().isoformat(),
            finished_at=clock().isoformat(),
        ))
        session.add(Job(
            id="previous-period", kind=kind, status="failed",
            started_at="2020-09-06T01:00:00+00:00", finished_at="2020-09-06T02:00:00+00:00",
        ))
    assert await schedule.run() is None
    clock.advance(minutes=30)
    assert await schedule.run() is not None
    clock.advance(hours=2)
    assert await schedule.run() is not None
    clock.advance(hours=5)
    assert await schedule.run() is None
    assert len(state["digests"]) == 2  # Old API attempt consumes the first slot.
    assert state["collects"] == 0


@pytest.mark.asyncio
async def test_active_pipeline_or_manual_job_defers_without_creating_queued_work(store, monkeypatch):
    _, state, pipeline, schedule, _ = harness(store, monkeypatch)
    async with pipeline.lock:
        assert await schedule.run() is None
    with store.begin() as session:
        session.add(Job(id="manual", kind="daily"))
    for _ in range(5):
        assert await schedule.run() is None
    assert state["digests"] == []
    with store() as session:
        assert [job.id for job in session.scalars(select(Job))] == ["manual"]


@pytest.mark.asyncio
async def test_cron_and_recovery_checks_cannot_claim_duplicate_jobs(store, monkeypatch):
    _, state, pipeline, schedule, _ = harness(store, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    original = pipeline.collect

    async def waiting_collect():
        started.set()
        await release.wait()
        return await original()

    pipeline.collect = waiting_collect
    active = asyncio.create_task(schedule.run())
    await started.wait()
    other = DailySchedule(pipeline, clock=schedule.clock)
    assert await schedule.run() is None
    assert await other.run() is None
    release.set()
    assert await active
    assert len(state["digests"]) == 1
    with store() as session:
        assert len(session.scalars(select(Job)).all()) == 1


@pytest.mark.asyncio
async def test_cancellation_records_failed_attempt_and_restart_can_retry_after_delay(store, monkeypatch):
    clock, state, pipeline, schedule, rebuild = harness(store, monkeypatch)
    started = asyncio.Event()

    async def cancelled_collect():
        started.set()
        await asyncio.Future()

    pipeline.collect = cancelled_collect
    active = asyncio.create_task(schedule.run())
    await started.wait()
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    with store() as session:
        job = session.scalar(select(Job))
        assert job.status == "failed" and job.finished_at == clock().isoformat()
    _, schedule = rebuild()
    assert await schedule.run() is None
    clock.advance(minutes=30)
    assert await schedule.run()
    assert len(state["digests"]) == 1
    assert state["collects"] == 0


@pytest.mark.asyncio
async def test_legacy_interrupted_running_job_is_recovered_by_normal_pipeline_startup(store, monkeypatch):
    clock, state, _, _, rebuild = harness(store, monkeypatch)
    with store.begin() as session:
        session.add(Job(id="interrupted", kind="daily", started_at=clock().isoformat()))
    _, schedule = rebuild()
    with store() as session:
        assert session.get(Job, "interrupted").status == "failed"
    assert await schedule.run() is None
    clock.advance(minutes=30)
    assert await schedule.run()
    assert len(state["digests"]) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_api_registers_shared_cron_and_immediate_recovery_only_when_enabled(tmp_path, monkeypatch, enabled):
    class Scheduler:
        def __init__(self, **options):
            self.options, self.jobs, self.running = options, [], False
            instances.append(self)

        def add_job(self, fn, trigger, **options):
            self.jobs.append((fn, trigger, options))

        def start(self):
            self.running = True

        def shutdown(self, **kwargs):
            self.running = False

    instances = []
    monkeypatch.setattr(api, "AsyncIOScheduler", Scheduler)
    config = tmp_path / "config.toml"
    config.write_text('timezone="Pacific/Kiritimati"\ndaily_hour=9\ndaily_minute=23\n')
    settings = Settings(
        config_path=str(config), database_url=f"sqlite:///{tmp_path}/api.db",
        reader_token="reader", scheduler_enabled=enabled,
    )
    with TestClient(api.create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        scheduler = instances[0]
        assert scheduler.options == {"timezone": "Pacific/Kiritimati"}
        if enabled:
            assert len(scheduler.jobs) == 3
            cron, recovery = scheduler.jobs[1:]
            assert cron[0].__self__ is recovery[0].__self__
            assert cron[1] == "cron" and cron[2]["hour"] == 9 and cron[2]["minute"] == 23
            assert recovery[1] == "interval" and recovery[2]["minutes"] == 5
            assert abs((recovery[2]["next_run_time"] - datetime.now(UTC)).total_seconds()) < 10
            assert cron[2]["max_instances"] == recovery[2]["max_instances"] == 1
            assert cron[2]["coalesce"] and recovery[2]["coalesce"]
        else:
            assert scheduler.jobs == []
    assert scheduler.running is False

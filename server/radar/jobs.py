"""A small durable, coalescing queue for the single API process.

Only queue intent is recovered. Translation/summary services retain ownership of
model reservations and must not replay an unknown or rejected model response.
"""

import asyncio
import fcntl
import logging
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, or_, select, update

from .models import Job

logger = logging.getLogger(__name__)
LANES = {"collect": ("collect",), "translate": ("translate",), "processing": ("read", "digest", "daily")}
PHASES = frozenset({"queued", "collect", "translate", "read", "presentation", "digest",
                    "completed", "retry_wait", "attention", "interrupted"})
PUBLIC_FIELDS = ("id", "kind", "status", "finished_at", "message", "queued_at", "phase",
                 "heartbeat_at", "progress_at", "retry_at", "attempt", "max_attempts", "reason_code")
_LIVE_OWNERS: set[str] = set()


class JobQueueConflict(ValueError):
    pass


def public_job(job):
    result = {key: getattr(job, key) for key in PUBLIC_FIELDS}
    result["started_at"] = job.run_started_at if job.queued_at else job.started_at
    return result


def job_counts(session):
    return dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())


def _write_lock(session):
    if session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _owner_alive(owner):
    if owner in _LIVE_OWNERS:
        return True
    try:
        prefix, pid, _ = owner.split(":", 2)
        if prefix != "api" or not pid.isdigit():
            return True  # An unknown owner is not proof of interruption.
        if int(pid) == os.getpid():
            return False
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (ValueError, PermissionError):
        return True


def _transport_failure(exc):
    if isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    code = getattr(exc, "status_code", None)
    return isinstance(code, int) and (code == 429 or 500 <= code <= 599)


class JobSupervisor:
    def __init__(self, pipeline, *, clock: Callable[[], datetime] | None = None,
                 heartbeat_seconds=15, continuation_seconds=30, poll_seconds=1, automatic=True):
        self.pipeline, self.sessions = pipeline, pipeline.sessions
        self.clock = clock or (lambda: datetime.now(UTC))
        self.owner = f"api:{os.getpid()}:{uuid4().hex}"
        self.heartbeat_seconds = heartbeat_seconds
        self.continuation_seconds = continuation_seconds
        self.poll_seconds = poll_seconds
        self.workers: dict[str, asyncio.Task] = {}
        self.events = {lane: asyncio.Event() for lane in LANES}
        self.last_kind: dict[str, str] = {}
        self.stopping = False
        self.automatic = automatic
        self.maintenance = None
        self.lock_file = None

    def _acquire_process_lock(self):
        if self.lock_file is not None:
            return
        with self.sessions() as session:
            path = session.bind.url.database
        if not path or path == ":memory:":
            return  # Isolated in-memory tests have no shared process database.
        stream = Path(path + ".jobs.lock").open("a+")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError("另一个后台执行器正在使用此数据库。") from None
        self.lock_file = stream

    def _now(self):
        return self.clock().astimezone(UTC)

    def submit(self, kind="collect", day=None, force=False, job_id=None, *, delay_seconds=0):
        if kind not in {kind for kinds in LANES.values() for kind in kinds}:
            raise JobQueueConflict("不支持的任务类型。")
        wanted_day = day.isoformat() if isinstance(day, date) else day
        if wanted_day is not None:
            try:
                wanted_day = date.fromisoformat(wanted_day).isoformat()
            except (TypeError, ValueError):
                raise JobQueueConflict("任务日期无效。") from None
        now = self._now().isoformat()
        with self.sessions.begin() as session:
            _write_lock(session)
            existing = session.scalar(select(Job).where(
                Job.kind == kind, Job.status.in_(("queued", "retrying")),
                Job.id != job_id if job_id else True,
            ).order_by(Job.queued_at, Job.id).limit(1))
            if existing:
                if existing.request_day != wanted_day:
                    raise JobQueueConflict("同类型任务已有其他日期排队，请等待完成。")
                existing.force = existing.force or force
                # New input may wake a delayed continuation; known failure backoff stays intact.
                if existing.status == "queued" and delay_seconds == 0:
                    existing.retry_at = None
                uid = existing.id
                if job_id:
                    supplied = session.get(Job, job_id)
                    if supplied and supplied.status == "queued":
                        supplied.status, supplied.phase = "completed", "completed"
                        supplied.finished_at = now
                        supplied.message = "已合并到同类型待办。"
            else:
                job = session.get(Job, job_id) if job_id else None
                if job_id and job is None:
                    raise JobQueueConflict("任务不存在。")
                if job is not None and job.owner:
                    raise JobQueueConflict("任务已被执行器接管。")
                job = job or Job(kind=kind)
                job.status, job.phase, job.kind = "queued", "queued", kind
                job.queued_at = job.queued_at or now
                job.request_day, job.force = wanted_day, bool(force)
                job.max_attempts = 1 if kind in ("daily", "digest") else 3
                job.retry_at = ((self._now() + timedelta(seconds=delay_seconds)).isoformat()
                                if delay_seconds else None)
                job.message = "等待后台执行。"
                session.add(job)
                session.flush()
                uid = job.id
        self._ensure_workers()
        self.events[self._lane(kind)].set()
        return uid

    async def schedule_collect(self):
        return self.submit("collect")

    @staticmethod
    def _lane(kind):
        return next(lane for lane, kinds in LANES.items() if kind in kinds)

    def _ensure_workers(self):
        if self.stopping:
            return
        self._acquire_process_lock()
        # API submissions are made on its event loop. Intent is already durable
        # if task construction fails; startup will drain it without a second submit.
        _LIVE_OWNERS.add(self.owner)
        for lane in LANES:
            if lane not in self.workers or self.workers[lane].done():
                coro = self._worker(lane)
                try:
                    self.workers[lane] = asyncio.create_task(coro, name=f"radar-jobs-{lane}")
                except BaseException:
                    coro.close()
                    raise

    def recover_interrupted(self, *, recover_legacy=False):
        """Restore intent under the process lock, without starting any worker.

        Deployment may use this only after proving the previous API process has
        exited. Call ``stop`` afterward to release the lock when not starting.
        """
        self._acquire_process_lock()
        now = self._now().isoformat()
        with self.sessions.begin() as session:
            _write_lock(session)
            # Only a deployment handoff with an independently verified stopped
            # old process may opt into this. It is never exposed on HTTP.
            if recover_legacy:
                restored = {
                    job.kind: job for job in session.scalars(select(Job).where(
                        Job.status.in_(("queued", "retrying")), Job.request_day.is_(None),
                        Job.kind.in_(("collect", "read", "translate")),
                    ).order_by(Job.queued_at, Job.id))
                }
                for row in session.scalars(select(Job).where(
                    Job.status == "running", Job.queued_at.is_(None), Job.owner == "",
                ).order_by(Job.started_at, Job.id)):
                    row.progress_at, row.reason_code = now, "restart_recovery"
                    if row.kind in ("daily", "digest"):
                        row.status, row.phase, row.finished_at = "needs_attention", "attention", now
                        row.message = "旧任务缺少原请求日期，已保留窗口记录，不自动猜测重放。"
                    elif row.kind in ("collect", "read", "translate"):
                        if row.kind in restored:
                            row.status, row.phase, row.reason_code = "completed", "completed", "coalesced"
                            row.finished_at = now
                            row.message = "旧进程已退出；重复恢复意图已合并，原任务记录保留。"
                        else:
                            row.queued_at, row.status, row.phase = now, "queued", "queued"
                            row.force = False
                            row.message = "已核验旧进程退出，等待从已保存进度恢复。"
                            restored[row.kind] = row
            for row in session.scalars(select(Job).where(Job.status == "running", Job.queued_at.is_not(None))):
                if not row.owner or _owner_alive(row.owner):
                    continue
                row.status, row.phase, row.reason_code = "queued", "queued", "restart_recovery"
                # A cancelled/interrupted invocation is not a known transport
                # failure. Model-level unknown reservations remain untouched.
                row.attempt = max(0, row.attempt - 1)
                row.owner, row.heartbeat_at, row.retry_at = "", None, None
                row.message = "上次进程中断，等待恢复已保存的处理进度。"
                row.progress_at = now
            # A crash/shutdown can leave both the interrupted invocation and its
            # already queued followup. Keep different dates distinct; combine
            # only compatible intent, preserving the stricter retry budget.
            kept = {}
            for row in session.scalars(select(Job).where(
                Job.status == "queued", Job.queued_at.is_not(None),
            ).order_by(Job.queued_at, Job.id)):
                key = row.kind, row.request_day
                if key not in kept:
                    kept[key] = row
                    continue
                first = kept[key]
                first.force = first.force or row.force
                first.attempt = max(first.attempt, row.attempt)
                first.max_attempts = min(first.max_attempts, row.max_attempts)
                first.retry_at = (min(first.retry_at, row.retry_at)
                                  if first.retry_at and row.retry_at else None)
                row.status, row.phase, row.reason_code = "completed", "completed", "coalesced"
                row.finished_at, row.progress_at, row.owner = now, now, ""
                row.message = "重复恢复意图已合并，原任务和重试记录保留。"

    async def start(self, *, recover_legacy=False):
        self.stopping = False
        self.recover_interrupted(recover_legacy=recover_legacy)
        self._ensure_workers()
        # No unconditional model jobs: these are read-only eligibility checks.
        if self.automatic:
            self._enqueue_eligible()
            self._collect_if_due()
            self.maintenance = asyncio.create_task(self._maintain(), name="radar-job-recovery")

    async def stop(self):
        self.stopping = True
        tasks = list(self.workers.values()) + ([self.maintenance] if self.maintenance else [])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        _LIVE_OWNERS.discard(self.owner)
        if self.lock_file is not None:
            fcntl.flock(self.lock_file, fcntl.LOCK_UN)
            self.lock_file.close()
            self.lock_file = None

    async def _maintain(self):
        while not self.stopping:
            await asyncio.sleep(60)
            try:
                self._ensure_workers()
                self._enqueue_eligible()
                self._collect_if_due()
            except Exception as exc:
                logger.warning("Job recovery check failed: %s", type(exc).__name__)

    def _active(self, kind):
        with self.sessions() as session:
            return bool(session.scalar(select(Job.id).where(
                Job.kind == kind, Job.status.in_(("queued", "running", "retrying")),
            ).limit(1)))

    def _collect_if_due(self):
        if self._active("collect"):
            return
        with self.sessions() as session:
            last = session.scalar(select(Job).where(Job.kind.in_(("collect", "daily"))).order_by(
                Job.started_at.desc(),
            ).limit(1))
            if last:
                try:
                    recent = datetime.fromisoformat(last.finished_at or last.started_at)
                    if recent.tzinfo is None or self._now() - recent < timedelta(
                        minutes=self.pipeline.config.collect_minutes,
                    ):
                        return
                except ValueError:
                    return
        self.submit("collect")

    def _enqueue_eligible(self):
        check = getattr(self.pipeline, "has_pending", None)
        if check is None:
            return
        for kind in ("translate", "read"):
            with self.sessions() as session:
                last = session.scalar(select(Job).where(Job.kind == kind).order_by(
                    Job.queued_at.desc(), Job.started_at.desc(),
                ).limit(1))
                blocked = last and last.status == "needs_attention" and last.reason_code in (
                    "task_error", "retry_exhausted",
                )
            if not blocked and not self._active(kind) and check(kind):
                self.submit(kind)

    def _claim(self, lane):
        now = self._now().isoformat()
        with self.sessions.begin() as session:
            _write_lock(session)
            # Formal standalone CLI work remains exclusive. A legacy unknown
            # running row cannot be declared dead just because it lacks a heartbeat.
            if lane != "collect" and session.scalar(select(Job.id).where(
                Job.status == "running", or_(Job.owner == "", Job.owner.is_(None)),
            ).limit(1)):
                return None
            if session.scalar(select(Job.id).where(
                Job.status == "running", Job.kind.in_(LANES[lane]),
            ).limit(1)):
                return None
            rows = session.scalars(select(Job).where(
                Job.kind.in_(LANES[lane]), Job.status.in_(("queued", "retrying")),
                or_(Job.retry_at.is_(None), Job.retry_at <= now),
            ).order_by(Job.queued_at, Job.id)).all()
            if not rows:
                return None
            # Rotate ready kinds; one read continuation cannot starve a due digest.
            row = next((item for item in rows if item.kind != self.last_kind.get(lane)), rows[0])
            if row.attempt >= row.max_attempts:
                row.status, row.phase, row.reason_code = "needs_attention", "attention", "retry_exhausted"
                row.finished_at, row.message = now, "自动恢复次数已用完，请查看失败原因。"
                return None
            row.status, row.phase, row.owner = "running", "queued", self.owner
            row.attempt += 1
            row.started_at = row.run_started_at = row.heartbeat_at = row.progress_at = now
            row.finished_at = row.retry_at = None
            row.more_pending = False
            row.message = "后台任务已开始。"
            self.last_kind[lane] = row.kind
            return row.id, row.kind, row.request_day, row.force

    async def _heartbeat(self, uid):
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            with self.sessions.begin() as session:
                session.execute(update(Job).where(
                    Job.id == uid, Job.owner == self.owner, Job.status == "running",
                ).values(heartbeat_at=self._now().isoformat()))

    async def _phase(self, uid, phase):
        if phase not in PHASES:
            raise ValueError("任务阶段无效。")
        with self.sessions.begin() as session:
            result = session.execute(update(Job).where(
                Job.id == uid, Job.owner == self.owner, Job.status == "running",
            ).values(phase=phase, progress_at=self._now().isoformat()))
            if result.rowcount != 1:
                raise RuntimeError("任务执行权已变更。")

    def _finish(self, uid):
        with self.sessions.begin() as session:
            _write_lock(session)
            row = session.get(Job, uid)
            if not row or row.owner != self.owner:
                return None
            # Pipeline records completed/attention status and stage results.
            if row.status == "running":
                row.status = "completed"
            row.phase = "completed" if row.status == "completed" else "attention"
            row.finished_at = row.finished_at or self._now().isoformat()
            row.heartbeat_at = self._now().isoformat()
            row.owner = ""
            return row.kind, row.status, row.more_pending

    def _failure(self, uid, exc):
        now = self._now()
        with self.sessions.begin() as session:
            _write_lock(session)
            row = session.get(Job, uid)
            if not row or row.owner != self.owner:
                return
            row.owner = ""
            row.progress_at, row.finished_at = now.isoformat(), now.isoformat()
            if isinstance(exc, asyncio.CancelledError):
                row.status, row.phase, row.reason_code = "queued", "interrupted", "cancelled"
                row.attempt = max(0, row.attempt - 1)
                row.retry_at = None
                row.message = "服务停止，待重启后从已保存进度恢复；未知模型请求不会重发。"
            elif _transport_failure(exc) and row.attempt < row.max_attempts:
                row.status, row.phase, row.reason_code = "retrying", "retry_wait", "transport_error"
                row.retry_at = (now + timedelta(seconds=(60 if row.attempt == 1 else 300))).isoformat()
                row.message = "连接暂时失败，已安排有限次数的后台恢复。"
            else:
                row.status, row.phase = "needs_attention", "attention"
                row.reason_code = "retry_exhausted" if _transport_failure(exc) else "task_error"
                row.message = "任务需要检查；已保存进度，不会反复重试未知或审核未通过的模型请求。"

    async def _worker(self, lane):
        while not self.stopping:
            event = self.events[lane]
            event.clear()
            claimed = self._claim(lane)
            if not claimed:
                try:
                    await asyncio.wait_for(event.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
                continue
            uid, kind, day, force = claimed
            heartbeat = asyncio.create_task(self._heartbeat(uid))
            try:
                await self.pipeline.run(
                    kind=kind, day=date.fromisoformat(day) if day else None, force=force, job_id=uid,
                    phase_callback=lambda phase, key=uid: self._phase(key, phase),
                )
                result = self._finish(uid)
                if result and result[1] == "completed":
                    if result[2]:
                        self.submit(kind, day=day, delay_seconds=self.continuation_seconds)
                    if kind in ("collect", "daily"):
                        self._enqueue_eligible()
            except asyncio.CancelledError as exc:
                self._failure(uid, exc)
                raise
            except Exception as exc:
                self._failure(uid, exc)
                logger.warning("Job worker %s failed: %s", lane, type(exc).__name__)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

"""Recover the latest due daily edition without replaying an unlimited backlog."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from .models import Digest, Job
from .pipeline import Pipeline, digest_window

DAILY_CHECK_MINUTES = 5
MAX_DAILY_ATTEMPTS = 3
RETRY_DELAYS = (timedelta(minutes=30), timedelta(hours=2))


def timestamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
        return parsed.astimezone(UTC) if parsed and parsed.tzinfo else None
    except ValueError:
        return None


class DailySchedule:
    def __init__(self, pipeline: Pipeline, *, clock: Callable[[], datetime] | None = None, submit=None):
        self.pipeline = pipeline
        self.submit = submit
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = asyncio.Lock()

    def due_window(self, now: datetime):
        config = self.pipeline.config
        day = now.astimezone(ZoneInfo(config.timezone)).date()
        start, end = digest_window(day, config)
        # Compare instants rather than wall-clock hours at DST transitions.
        if now < end:
            day -= timedelta(days=1)
            start, end = digest_window(day, config)
        return day, start, end

    async def run(self):
        # A later check will retry after active collection/manual work finishes.
        # Do not stack automatic runs behind the Pipeline's work queue.
        if self.lock.locked() or (self.submit is None and self.pipeline.lock.locked()):
            return None
        async with self.lock:
            now = self.clock().astimezone(UTC)
            day, start, end = self.due_window(now)
            _, next_end = digest_window(day + timedelta(days=1), self.pipeline.config)
            ids = [str(uuid5(NAMESPACE_URL, f"ai-radar:daily:{start.isoformat()}:{end.isoformat()}:{n}"))
                   for n in range(1, MAX_DAILY_ATTEMPTS + 1)]
            try:
                with self.pipeline.sessions.begin() as session:
                    # A published no_updates edition is also a completed result.
                    # Never replace a published edition automatically, including
                    # one written by a manual operation or older configuration.
                    if session.get(Digest, day.isoformat()) is not None:
                        return None
                    if self.submit is not None and session.scalar(select(Job.id).where(
                        Job.kind.in_(("daily", "digest")), Job.status.in_(("queued", "retrying")),
                    ).limit(1)):
                        # Preserve an explicitly requested older date instead of
                        # creating another same-kind pending request behind it.
                        return None
                    if self.submit is None and session.scalar(
                        select(Job.id).where(Job.status == "running").limit(1)
                    ):
                        return None
                    rows = session.scalars(select(Job).where(
                        or_(Job.id.in_(ids), Job.kind.in_(("daily", "digest"))),
                    )).all()
                    history = []
                    for row in rows:
                        began = timestamp(row.started_at)
                        # Legacy/manual jobs do not store their requested day.
                        # Conservatively charge all daily/digest work begun in
                        # this delivery period against its automatic retry cap.
                        if row.id in ids or (began is not None and end <= began < next_end):
                            history.append(row)
                    if len(history) >= MAX_DAILY_ATTEMPTS:
                        return None
                    if any(row.status in ("queued", "running", "retrying") for row in history):
                        return None
                    if history:
                        finished = [timestamp(row.finished_at or row.started_at) for row in history]
                        if any(value is None for value in finished):
                            return None  # Unknown completion time must not trigger an immediate retry.
                        if now < max(finished) + RETRY_DELAYS[len(history) - 1]:
                            return None
                    attempt = len(history) + 1
                    uid = ids[attempt - 1]
                    if any(row.id == uid for row in history):
                        return None
                    session.add(Job(
                        id=uid, kind="daily" if attempt == 1 else "digest",
                        status="queued" if self.submit else "running",
                        queued_at=now.isoformat() if self.submit else None,
                        request_day=day.isoformat() if self.submit else None,
                        phase="queued" if self.submit else "",
                        max_attempts=1 if self.submit else 3,
                        started_at=now.isoformat(),
                        message=f"自动日报 {day.isoformat()}，本窗口第 {attempt}/{MAX_DAILY_ATTEMPTS} 次尝试。",
                    ))
            except IntegrityError:
                # The deterministic job ID is the durable claim if another
                # scheduler checks this window concurrently.
                return None
            if self.submit is not None:
                return self.submit(
                    kind="daily" if attempt == 1 else "digest", day=day, force=False, job_id=uid,
                )
            try:
                return await self.pipeline.run(
                    kind="daily" if attempt == 1 else "digest", day=day, force=False, job_id=uid,
                )
            except BaseException:
                # Pipeline normally records failures itself. Cancellation during
                # shutdown must also leave a terminal, counted attempt behind.
                with self.pipeline.sessions.begin() as session:
                    row = session.get(Job, uid)
                    if row and row.status == "running":
                        row.status = "failed"
                        row.finished_at = self.clock().astimezone(UTC).isoformat()
                        row.message = "自动日报任务中断；后续调度会在本窗口剩余次数内重试。"
                raise

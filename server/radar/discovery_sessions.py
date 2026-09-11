"""FIFO discovery batches on a reusable, role-bound Codex conversation.

The database owns the queue and budgets. A database-specific OS lock, inherited
by Codex, fences live processes even after the API dies. Expiry never steals it.
"""

import asyncio
import fcntl
import os
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select

from . import codex_sessions, model_router, usage
from .discovery_contracts import DiscoveryBatchDecision, batch_prompt, validate_batch
from .models import AgentBatch, AgentSession, DiscoveryCall, DiscoveryCandidate, now_iso

SCOPE = "discovery"
MAX_PROMPT_CHARS = 120_000


class DiscoverySessions:
    def __init__(self, service):
        self.service = service
        self.sessions = service.sessions
        self.settings = service.settings
        self.base_provider = service.provider_config
        self.routed = model_router.active(self.base_provider)
        self.provider = (model_router.provider_for(self.base_provider, "confirmation")
            if self.routed else self.base_provider)
        self.schema = (model_router.confirmation_schema(DiscoveryBatchDecision)
            if self.routed else DiscoveryBatchDecision)

    def _prompt(self, batch_id, members):
        prompt = batch_prompt(batch_id, members)
        return model_router.confirmation_prompt(prompt) if self.routed else prompt

    @contextmanager
    def _exclusive(self):
        with self.sessions() as session:
            if session.bind.dialect.name != "sqlite":
                raise RuntimeError("Persistent discovery sessions require the supported SQLite deployment")
            database = session.bind.url.database
        path = (Path(database).resolve().with_name(Path(database).name + ".discovery.lock")
            if database and database != ":memory:"
            else Path(self.settings.session_directory).resolve() / ".discovery.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            # Close only: LOCK_UN here would also unlock a surviving child's FD.
            yield fd
        finally:
            os.close(fd)

    def has_recovery(self, session):
        return bool(session.scalar(select(AgentBatch.id).where(
            AgentBatch.scope == SCOPE, AgentBatch.status == "reserved",
        ).limit(1)))

    def status(self, session):
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        counts = dict(session.execute(select(AgentBatch.status, func.count()).where(
            AgentBatch.scope == SCOPE,
        ).group_by(AgentBatch.status)).all())
        active = session.scalar(select(AgentSession).where(
            AgentSession.scope == SCOPE, AgentSession.status.in_(("idle", "running")),
        ).order_by(AgentSession.created_at.desc()).limit(1))
        oldest = session.scalar(select(DiscoveryCandidate.created_at).where(
            DiscoveryCandidate.status.in_(("pending", "retry_wait")),
        ).order_by(DiscoveryCandidate.created_at).limit(1))
        return {"enabled": self.settings.session_reuse and self.provider.kind == "codex",
            "scope": SCOPE, "batch_size": self.settings.batch_size, "batches": counts,
            "turns_today": session.scalar(select(func.count()).select_from(AgentBatch).where(
                AgentBatch.scope == SCOPE, AgentBatch.created_at >= today)),
            "session_id": active.cli_session_id if active else None,
            "session_state": active.status if active else "none", "session_turns": active.turns if active else 0,
            "max_turns": self.settings.session_max_turns, "max_age_hours": self.settings.session_max_age_hours,
            "oldest_waiting_at": oldest, "budget_unit": "candidate_decision"}

    def _reserve(self, limit):
        from .discovery import _hash

        batch_id, owner = str(uuid4()), str(uuid4())
        with self.service._transaction() as session:
            if self.has_recovery(session):
                return None
            members = []
            rows = session.scalars(select(DiscoveryCandidate).where(
                DiscoveryCandidate.status.in_(("pending", "retry_wait")),
            ).order_by(DiscoveryCandidate.created_at, DiscoveryCandidate.id))
            identity = {"kind": "codex", "model": self.provider.model,
                "config_fingerprint": _hash(self.provider.model_dump(mode="json")),
                "request_upper_bound": None, "batch_id": batch_id}
            lease = (datetime.now(UTC) + timedelta(seconds=self.base_provider.timeout_seconds + 60)).isoformat()
            for row in rows:
                if len(members) >= limit:
                    break
                if not self.service._eligible(session, row, now_iso()):
                    continue
                member = {"candidate_id": row.id, "fingerprint": row.fingerprint,
                    "payload": deepcopy(row.payload), "metrics": deepcopy(row.latest_metrics), "call_id": str(uuid4())}
                if len(self._prompt(batch_id, [member])) > MAX_PROMPT_CHARS:
                    row.status, row.error_code, row.updated_at = "needs_attention", "prompt_too_large", now_iso()
                    continue
                if len(self._prompt(batch_id, [*members, member])) > MAX_PROMPT_CHARS:
                    break  # Keep FIFO; this candidate starts the next batch.
                members.append(member)
                row.status, row.owner, row.provider_identity = "reserved", owner, deepcopy(identity)
                row.lease_until, row.retry_at, row.error_code, row.updated_at = lease, "", "", now_iso()
                session.add(DiscoveryCall(id=member["call_id"], candidate_id=row.id, fingerprint=row.fingerprint,
                    owner=owner, provider=deepcopy(identity)))
                session.flush()  # Each candidate consumes its existing daily/pool unit before the next selection.
            if not members:
                return None
            configuration = _hash({"provider": self.provider.model_dump(mode="json"),
                "policy": self.settings.policy, "schema": self.schema.model_json_schema(), "scope": SCOPE,
                "instructions": self._prompt("00000000-0000-0000-0000-000000000000", [])})
            active = None
            for conversation in session.scalars(select(AgentSession).where(
                AgentSession.scope == SCOPE, AgentSession.status.in_(("idle", "running")),
            ).order_by(AgentSession.created_at.desc())):
                reason = ("configuration_changed" if conversation.configuration != configuration else
                    "turn_limit" if conversation.turns >= self.settings.session_max_turns else
                    "age_limit" if datetime.fromisoformat(conversation.created_at) < datetime.now(UTC)
                        - timedelta(hours=self.settings.session_max_age_hours) else
                    "session_unavailable" if not conversation.cli_session_id or conversation.status != "idle" else
                    "duplicate_session" if active else "")
                if reason:
                    conversation.status, conversation.close_reason, conversation.updated_at = "closed", reason, now_iso()
                else:
                    active = conversation
            if active is None:
                active = AgentSession(scope=SCOPE, configuration=configuration)
                session.add(active)
                session.flush()
            prompt = self._prompt(batch_id, members)
            expected_thread = active.cli_session_id or None
            batch = AgentBatch(id=batch_id, session_id=active.id, scope=SCOPE, owner=owner, members=members,
                prompt=prompt, workdir=str(Path(self.settings.session_directory).resolve() / batch_id),
                request_fingerprint=codex_sessions.request_fingerprint(
                    self.provider, prompt, self.schema, session_id=expected_thread), lease_until=lease)
            session.add(batch)
            active.status, active.batch_id, active.updated_at = "running", batch_id, now_iso()
            active.turns += 1
            session.flush()
            return batch, expected_thread

    async def _bind_thread(self, batch_id, thread_id):
        with self.service._transaction() as session:
            batch = session.get(AgentBatch, batch_id)
            conversation = session.get(AgentSession, batch.session_id)
            if batch.status != "reserved" or conversation.batch_id != batch_id or (
                conversation.cli_session_id and conversation.cli_session_id != thread_id
            ):
                raise ValueError("Discovery session owner mismatch")
            conversation.cli_session_id, conversation.updated_at = thread_id, now_iso()

    def _finish(self, batch_id, response, resolved_text=None):
        with self.service._transaction() as session:
            batch = session.get(AgentBatch, batch_id)
            if batch.status != "reserved":
                return []
            conversation = session.get(AgentSession, batch.session_id)
            if response.request_fingerprint != batch.request_fingerprint or (
                conversation.cli_session_id and conversation.cli_session_id != response.session_id
            ) or conversation.batch_id != batch_id:
                raise ValueError("Discovery batch receipt does not match")
            decision = validate_batch(resolved_text if resolved_text is not None else response.text, batch_id, batch.members)
            by_id = {value.candidate_id: value.model_dump(mode="json") for value in decision.decisions}
            accepted = []
            for member in batch.members:
                row = session.get(DiscoveryCandidate, member["candidate_id"])
                call = session.get(DiscoveryCall, member["call_id"])
                if (row is None or call is None or row.owner != batch.owner or row.status != "reserved"
                        or row.fingerprint != member["fingerprint"] or call.owner != batch.owner
                        or call.status != "reserved" or call.provider.get("batch_id") != batch_id):
                    raise ValueError("Discovery candidate owner mismatch")
                value = by_id[row.id]
                call.status, call.result, call.completed_at = "completed", deepcopy(value), now_iso()
                row.result, row.judged_at, row.updated_at, row.lease_until = deepcopy(value), now_iso(), now_iso(), ""
                row.status = "accepted" if self.service._latest(session, row) else "superseded"
                if row.status == "accepted":
                    accepted.append(row.id)
            batch.status, batch.result, batch.completed_at, batch.lease_until = (
                "completed", decision.model_dump(mode="json"), now_iso(), "")
            conversation.cli_session_id, conversation.status = response.session_id, "idle"
            conversation.batch_id, conversation.updated_at = "", now_iso()
            return accepted

    def _fail(self, batch_id, code="outcome_unknown", unknown=True):
        with self.service._transaction() as session:
            batch = session.get(AgentBatch, batch_id)
            if batch.status != "reserved":
                return
            state = "unknown" if unknown else "needs_attention"
            batch.status, batch.error_code, batch.completed_at, batch.lease_until = state, code, now_iso(), ""
            conversation = session.get(AgentSession, batch.session_id)
            conversation.status, conversation.close_reason, conversation.updated_at = "closed", code, now_iso()
            for member in batch.members:
                row = session.get(DiscoveryCandidate, member["candidate_id"])
                call = session.get(DiscoveryCall, member["call_id"])
                if call and call.status == "reserved" and call.owner == batch.owner:
                    call.status, call.error_code, call.completed_at = "unknown" if unknown else "failed", code, now_iso()
                if row and row.status == "reserved" and row.owner == batch.owner:
                    row.status, row.error_code, row.updated_at, row.lease_until = state, code, now_iso(), ""

    def _reconcile_locked(self):
        with self.sessions() as session:
            batches = list(session.scalars(select(AgentBatch).where(
                AgentBatch.scope == SCOPE, AgentBatch.status == "reserved",
            ).order_by(AgentBatch.created_at)))
        for batch in batches:
            # Owning the process lock proves no cooperating CLI still runs, regardless of lease time.
            try:
                routed = batch.prompt.startswith(model_router.PREFIX)
                schema = model_router.confirmation_schema(DiscoveryBatchDecision) if routed else DiscoveryBatchDecision
                receipts = codex_sessions.scan_artifacts(batch.workdir, schema_type=schema)
                matches = [receipt for receipt in receipts if receipt.request_fingerprint == batch.request_fingerprint]
                if len(matches) == 1:
                    resolved = None
                    if routed:
                        value = model_router.parse_confirmation(matches[0].text, DiscoveryBatchDecision, batch.prompt)
                        if value.gate.decision == "needs_adjudication":
                            continue  # The async queue resumes the one durable adjudication turn.
                        resolved = value.result.model_dump_json()
                    self._finish(batch.id, matches[0], resolved)
                else:
                    self._fail(batch.id)
            except (ValueError, TypeError, KeyError):
                self._fail(batch.id, "format_invalid", unknown=False)
            except (codex_sessions.CodexSessionError, OSError):
                self._fail(batch.id)
        return len(batches)

    def recover(self):
        with self._exclusive() as fd:
            return self._reconcile_locked() if fd is not None else 0

    async def _resume_adjudication(self, fd):
        with self.sessions() as session:
            batches = list(session.scalars(select(AgentBatch).where(
                AgentBatch.scope == SCOPE, AgentBatch.status == "reserved",
            ).order_by(AgentBatch.created_at)))
        judged = 0
        for batch in batches:
            if not batch.prompt.startswith(model_router.PREFIX):
                continue
            try:
                receipts = codex_sessions.scan_artifacts(batch.workdir,
                    schema_type=model_router.confirmation_schema(DiscoveryBatchDecision))
                receipt = next(r for r in receipts if r.request_fingerprint == batch.request_fingerprint)
                with usage.scope("discovery_foresight"):
                    text = await model_router.finish_confirmation(self.base_provider,
                        model_router.original_prompt(batch.prompt), DiscoveryBatchDecision, receipt.text,
                        batch.workdir, lock_fd=fd)
                self._finish(batch.id, receipt, text)
                judged += len(batch.members)
            except (ValueError, TypeError, KeyError, StopIteration):
                self._fail(batch.id, "format_invalid", unknown=False)
            except codex_sessions.CodexSessionError as exc:
                self._fail(batch.id, "outcome_unknown" if exc.outcome_unknown else "provider_unavailable",
                    unknown=exc.outcome_unknown)
        return judged

    async def pending(self, limit=None):
        result = {"processed": 0, "judged": 0, "applied": 0, "failed": 0}
        limit = max(0, min(self.settings.batch_size, limit if limit is not None else self.settings.batch_size))
        with self._exclusive() as fd:
            if fd is None:
                return {**result, "more_pending": True}
            self._reconcile_locked()
            if limit:
                result["judged"] += await self._resume_adjudication(fd)
            with self.sessions() as session:
                accepted = [row.id for row in session.scalars(select(DiscoveryCandidate).where(
                    DiscoveryCandidate.status == "accepted",
                ).order_by(DiscoveryCandidate.created_at, DiscoveryCandidate.id))
                    if self.service._eligible(session, row, now_iso())][:limit]
            for candidate_id in accepted:
                applied = self.service._apply(candidate_id)
                result["processed"] += 1
                result["applied"] += int(applied)
                result["failed"] += int(not applied)
            reservation = self._reserve(limit - len(accepted))
            if reservation:
                batch, expected_thread = reservation
                result["processed"] += len(batch.members)
                try:
                    with usage.scope("discovery_foresight"):
                        stage = (model_router.stage_for("generation", "confirmation", model_router.config().confirmation)
                            if self.routed else "generation")
                        with usage.scope("discovery_foresight", stage):
                            response = await codex_sessions.run(self.provider, batch.workdir, batch.prompt,
                                self.schema, session_id=expected_thread, lock_fd=fd,
                                on_thread=lambda thread_id: self._bind_thread(batch.id, thread_id))
                        resolved = (await model_router.finish_confirmation(self.base_provider,
                            model_router.original_prompt(batch.prompt), DiscoveryBatchDecision, response.text,
                            batch.workdir, lock_fd=fd)) if self.routed else None
                except BaseException as exc:
                    if isinstance(exc, codex_sessions.CodexSessionError) and not exc.outcome_unknown:
                        self._fail(batch.id, "format_invalid" if exc.failure_kind == "format" else
                            "provider_unavailable", unknown=False)
                    else:
                        self._fail(batch.id)
                    result["failed"] += len(batch.members)
                    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                        raise
                else:
                    try:
                        accepted = self._finish(batch.id, response, resolved)
                    except (ValueError, TypeError, KeyError):
                        self._fail(batch.id, "format_invalid", unknown=False)
                        result["failed"] += len(batch.members)
                    else:
                        result["judged"] += len(batch.members)
                        for candidate_id in accepted:
                            applied = self.service._apply(candidate_id)
                            result["applied"] += int(applied)
                            result["failed"] += int(not applied)
        return {**result, "more_pending": self.service.has_pending()}

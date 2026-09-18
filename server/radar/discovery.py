"""Durable one-decision discovery. Unknown calls are never replayed automatically."""

import asyncio
import hashlib
import json
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select

from . import usage
from .admission import evaluate
from .discovery_contracts import DiscoveryDecision, decision_prompt, validate_decision
from .discovery_priority import day_start, deadline, ordered
from .discovery_priority import score as priority_score
from .models import DiscoveryCall, DiscoveryCandidate, now_iso
from .providers import make_provider
from .ranking import article_id, classify, engagement

OPEN = ("pending", "retry_wait", "reserved", "accepted")
RETRY_SECONDS = 900
MAX_PROMPT_CHARS = 120_000


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _pool_limit(total, low_engagement):
    # Fixed shares: the odd slot belongs to low-engagement discovery, never borrowed.
    return (total + 1) // 2 if low_engagement else total // 2


def _lock(session):
    if session.bind.dialect.name == "sqlite":
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")


def source_fingerprint(payload):
    fields = ("platform", "external_id", "url", "title", "text", "published_at", "author_external_id")
    data = {key: payload.get(key, "") for key in fields}
    data["references"] = sorted(payload.get("references", []), key=lambda row: json.dumps(row, sort_keys=True))
    data["entities"] = sorted([
        {key: entity.get(key) for key in ("platform", "external_id", "handle", "relation", "matched_text")}
        for entity in payload.get("entities", [])
    ], key=lambda row: json.dumps(row, sort_keys=True))
    return _hash(data)


def _qualified(item, config, priority):
    text = re.sub(r"https?://\S+", "", item.text).strip()
    size = len(text)
    links = any(ref.kind == "link" for ref in item.references) or bool(re.search(r"https?://\S+", item.text))
    signal = bool(re.search(r"\b(paper|benchmark|dataset|weights|repository|release[sd]?|research)\b|论文|基准测试|数据集|开源|权重|实验|发布", text, re.I))
    concrete = size >= 80 or (size >= 30 and (links or signal))
    hot = priority or engagement(item.metrics) >= config.discovery.hot_engagement
    if hot:
        return bool(item.entities) or concrete
    return item.platform in {"x", "facebook"} and concrete


def queue_candidate(session, item, config, source_priority=False):
    settings = config.discovery
    now = datetime.now(UTC)
    if not settings.enabled:
        return None
    payload = item.model_dump(mode="json")
    fingerprint = source_fingerprint(payload)
    key = article_id(item)
    uid = _hash([key, fingerprint])
    _lock(session)
    row = session.get(DiscoveryCandidate, uid)
    score = engagement(item.metrics)
    if row:
        was_seed = row.seed_qualified
        row.latest_metrics, row.latest_engagement = dict(item.metrics), score
        row.source_priority = row.source_priority or source_priority
        row.seed_qualified = row.seed_qualified or source_priority or score >= settings.hot_engagement
        if not was_seed and row.seed_qualified and row.status == "done" and row.result:
            # Becoming a hot seed can apply already-grounded entity recommendations.
            # It never grants another model call or changes the original decision.
            row.status, row.retry_at, row.error_code = "accepted", "", ""
        row.updated_at = now.isoformat()
        return row
    first = session.scalar(select(DiscoveryCandidate).where(DiscoveryCandidate.article_key == key).order_by(
        DiscoveryCandidate.created_at, DiscoveryCandidate.id,
    ).limit(1))
    # Keep an article in its first pool even after metrics, priority or content change.
    low_engagement = first.low_engagement if first else not source_priority and score < config.min_engagement
    # A changed source invalidates unapplied judgments even if this version is not eligible.
    for previous in session.scalars(select(DiscoveryCandidate).where(
        DiscoveryCandidate.article_key == key, DiscoveryCandidate.status.in_(OPEN),
    )):
        if previous.status != "reserved":
            previous.status = "superseded"
        previous.error_code, previous.updated_at = "source_changed", now.isoformat()
    if not evaluate(item.platform, item.text, payload.get("references", []))[0]:
        return None
    if not classify(item) or not (
        now - timedelta(hours=settings.max_age_hours) <= item.published_at <= now + timedelta(minutes=5)
    ) or not _qualified(item, config, source_priority):
        return None
    today = day_start(config, now).isoformat()
    daily = session.scalar(select(func.count()).select_from(DiscoveryCandidate).where(
        DiscoveryCandidate.created_at >= today, DiscoveryCandidate.status != "not_selected"))
    pool_daily = session.scalar(select(func.count()).select_from(DiscoveryCandidate).where(
        DiscoveryCandidate.created_at >= today, DiscoveryCandidate.low_engagement.is_(low_engagement),
    ))
    # Age out uncalled work explicitly; old rows and spent calls remain in history.
    for previous in session.scalars(select(DiscoveryCandidate).where(DiscoveryCandidate.status.in_(("pending", "retry_wait")))):
        if deadline(previous, config) <= now and not session.scalar(select(DiscoveryCall.id).where(
                DiscoveryCall.candidate_id == previous.id, DiscoveryCall.status.in_(("reserved", "unknown"))).limit(1)):
            previous.status, previous.error_code, previous.updated_at = "expired", "freshness_deadline" if settings.freshness_enabled else "source_expired", now.isoformat()
    pending = session.scalar(select(func.count()).select_from(DiscoveryCandidate).where(DiscoveryCandidate.status.in_(OPEN)))
    if settings.freshness_enabled and (daily >= settings.max_candidates_per_day or pending >= settings.max_pending):
        # Replace only uncalled, same-day work. Preserve spent/unknown calls and their evidence.
        uncalled = list(session.scalars(select(DiscoveryCandidate).where(
            DiscoveryCandidate.status == "pending", DiscoveryCandidate.created_at >= today,
            ~select(DiscoveryCall.id).where(DiscoveryCall.candidate_id == DiscoveryCandidate.id).exists())))
        incoming = SimpleNamespace(payload=payload, source_priority=source_priority,
                                   latest_engagement=score, low_engagement=low_engagement)
        weakest = min(uncalled, key=lambda r: priority_score(r, now), default=None)
        if weakest is not None and priority_score(incoming, now) > priority_score(weakest, now):
            weakest.status, weakest.error_code, weakest.updated_at = "not_selected", "priority_replaced", now.isoformat()
            daily -= 1
            pending -= 1
    if (daily >= settings.max_candidates_per_day or pending >= settings.max_pending
            or (not settings.freshness_enabled and pool_daily >= _pool_limit(settings.max_candidates_per_day, low_engagement))):
        return None
    row = DiscoveryCandidate(id=uid, article_key=key, fingerprint=fingerprint, payload=payload,
        latest_metrics=dict(item.metrics), source_priority=source_priority,
        seed_qualified=source_priority or score >= settings.hot_engagement,
        low_engagement=low_engagement,
        initial_engagement=score, latest_engagement=score, policy=settings.policy)
    session.add(row)
    session.flush()
    return row


class DiscoveryService:
    def __init__(self, sessions, config, publish_callback):
        self.sessions, self.config, self.publish_callback = sessions, config, publish_callback
        self.settings = config.discovery
        self.provider_config = self.settings.provider or config.provider
        self._token_snapshot = None

    def token_budget(self):
        current = datetime.now(UTC)
        since = day_start(self.config, current).isoformat()
        if not self._token_snapshot or self._token_snapshot[0] != since or (current - self._token_snapshot[1]).total_seconds() >= 5:
            self._token_snapshot = (since, current, usage.feature_usage('discovery_foresight', since))
        return self._token_snapshot[2]

    def ordered(self, rows):
        return ordered(rows, self.config)

    @contextmanager
    def _transaction(self):
        with self.sessions.begin() as session:
            _lock(session)
            yield session

    def _calls(self, session, candidate_id=None, *, low_engagement=None):
        query = select(func.count()).select_from(DiscoveryCall)
        if candidate_id:
            query = query.where(DiscoveryCall.candidate_id == candidate_id)
        else:
            today = day_start(self.config, datetime.now(UTC)).isoformat()
            query = query.where(DiscoveryCall.created_at >= today)
            if low_engagement is not None:
                query = query.join(DiscoveryCandidate, DiscoveryCall.candidate_id == DiscoveryCandidate.id).where(
                    DiscoveryCandidate.low_engagement.is_(low_engagement),
                )
        return session.scalar(query)

    def _latest(self, session, row):
        if row.error_code == "source_changed":
            return False
        other = session.scalar(select(DiscoveryCandidate.id).where(
            DiscoveryCandidate.article_key == row.article_key,
            DiscoveryCandidate.created_at > row.created_at,
        ).limit(1))
        return other is None

    def _eligible(self, session, row, now):
        if not evaluate(row.payload.get("platform"), row.payload.get("text", ""),
                        row.payload.get("references", []))[0]:
            return False
        if not self._latest(session, row):
            return False
        if row.status == "accepted":
            return bool(row.result) and (not row.retry_at or row.retry_at <= now)
        if row.status not in {"pending", "retry_wait"} or row.retry_at > now:
            return False
        if session.scalar(select(DiscoveryCall.id).where(
            DiscoveryCall.candidate_id == row.id, DiscoveryCall.status.in_(("reserved", "unknown")),
        ).limit(1)):
            return False
        if deadline(row, self.config) <= datetime.fromisoformat(now):
            return False
        if self.settings.freshness_enabled:
            hourly = session.scalar(select(func.count()).select_from(DiscoveryCall).where(
                DiscoveryCall.created_at >= (datetime.fromisoformat(now) - timedelta(hours=1)).isoformat()))
            budget = self.token_budget()
            # A soft admission budget, not a claim that unknown receipts cost zero.
            exposure = budget['tokens'] + budget['unknown_calls'] * 20_000
            return (self._calls(session, row.id) < min(2, self.settings.max_calls_per_candidate)
                    and self._calls(session) < self.settings.max_calls_per_day
                    and hourly < self.settings.max_calls_per_hour and exposure < self.settings.max_tokens_per_day)
        return (self._calls(session, row.id) < min(2, self.settings.max_calls_per_candidate)
            and self._calls(session) < self.settings.max_calls_per_day
            and self._calls(session, low_engagement=row.low_engagement)
            < _pool_limit(self.settings.max_calls_per_day, row.low_engagement))

    def has_pending(self):
        if not self.settings.enabled:
            return False
        now = now_iso()
        with self.sessions() as session:
            from .discovery_sessions import DiscoverySessions

            if DiscoverySessions(self).has_recovery(session):
                return True
            for row in session.scalars(select(DiscoveryCandidate).where(
                DiscoveryCandidate.status.in_(("accepted", "pending", "retry_wait", "reserved")),
            )):
                if (session.scalar(select(DiscoveryCall.id).where(
                        DiscoveryCall.candidate_id == row.id, DiscoveryCall.status.in_(("reserved", "unknown"))).limit(1))
                        and row.status != "reserved"):
                    continue
                if row.status == "reserved" and any(call.provider.get("batch_id") for call in session.scalars(
                    select(DiscoveryCall).where(DiscoveryCall.candidate_id == row.id),
                )):
                    continue  # Persistent batches have their own recovery/eligibility contract.
                if row.status == "reserved":
                    if row.lease_until <= now:
                        return True
                    continue
                if row.status in ("pending", "retry_wait") and (
                    deadline(row, self.config) <= datetime.fromisoformat(now)
                ):
                    return True  # Schedule a database-only expiry pass, even without new input.
                if self._eligible(session, row, now):
                    return True
            return False

    def status(self, session=None):
        if session is None:
            with self.sessions() as own:
                return self.status(own)
        counts = dict(session.execute(select(DiscoveryCandidate.status, func.count()).group_by(DiscoveryCandidate.status)).all())
        errors = dict(session.execute(select(DiscoveryCandidate.error_code, func.count()).where(
            DiscoveryCandidate.status.in_(("needs_attention", "unknown", "retry_wait", "accepted")),
            DiscoveryCandidate.error_code != "",
        ).group_by(DiscoveryCandidate.error_code)).all())
        today = day_start(self.config, datetime.now(UTC)).isoformat()
        daily = session.scalar(select(func.count()).select_from(DiscoveryCandidate).where(
            DiscoveryCandidate.created_at >= today, DiscoveryCandidate.status != "not_selected"))
        pools = {}
        for name, low in (("low_engagement", True), ("seed", False)):
            candidates = session.scalar(select(func.count()).select_from(DiscoveryCandidate).where(
                DiscoveryCandidate.created_at >= today, DiscoveryCandidate.low_engagement.is_(low),
                DiscoveryCandidate.status != "not_selected",
            ))
            calls = self._calls(session, low_engagement=low)
            candidate_limit = self.settings.max_candidates_per_day if self.settings.freshness_enabled else _pool_limit(self.settings.max_candidates_per_day, low)
            call_limit = self.settings.max_calls_per_day if self.settings.freshness_enabled else _pool_limit(self.settings.max_calls_per_day, low)
            pools[name] = {"candidates_today": candidates, "candidate_limit": candidate_limit,
                "calls_today": calls, "call_limit": call_limit,
                "candidate_limit_reached": candidates >= candidate_limit,
                "call_limit_reached": calls >= call_limit}
        from .discovery_sessions import DiscoverySessions

        return {"enabled": self.settings.enabled, "counts": counts, "errors": errors, "calls_today": self._calls(session),
                "selection": {"policy": "freshness" if self.settings.freshness_enabled else "fifo",
                    "max_wait_minutes": self.settings.max_wait_minutes if self.settings.freshness_enabled else None,
                    "timezone": self.config.timezone if self.settings.freshness_enabled else "UTC",
                    "hourly_limit": self.settings.max_calls_per_hour,
                    "token_budget": {**self.token_budget(), "limit": self.settings.max_tokens_per_day,
                                     "unknown_reservation_tokens": 20_000}},
                "sessions": DiscoverySessions(self).status(session),
                "pools": pools,
                "max_calls_per_day": self.settings.max_calls_per_day, "candidates_today": daily,
                "candidate_limit_reached": daily >= self.settings.max_candidates_per_day,
                "pending_limit_reached": sum(counts.get(s, 0) for s in OPEN) >= self.settings.max_pending,
                "max_pending": self.settings.max_pending, "max_candidates_per_day": self.settings.max_candidates_per_day}

    def _recover_legacy(self):
        recovered = 0
        now = now_iso()
        with self._transaction() as session:
            for row in session.scalars(select(DiscoveryCandidate).where(
                DiscoveryCandidate.status.in_(("pending", "retry_wait", "reserved")),
            )):
                calls = list(session.scalars(select(DiscoveryCall).where(DiscoveryCall.candidate_id == row.id)))
                if any(call.provider.get("batch_id") for call in calls) and (
                        not self.settings.freshness_enabled or row.status == "reserved"
                        or any(call.status in {"reserved", "unknown"} for call in calls)):
                    continue
                if row.status == "reserved":
                    if row.lease_until > now:
                        continue
                    row.status, row.error_code = "unknown", "outcome_unknown"
                    for call in calls:
                        if call.status == "reserved":
                            call.status, call.error_code, call.completed_at = "unknown", "outcome_unknown", now
                else:
                    if deadline(row, self.config) > datetime.fromisoformat(now):
                        continue
                    row.status, row.error_code = "expired", "freshness_deadline" if self.settings.freshness_enabled else "source_expired"
                row.owner, row.lease_until, row.retry_at, row.updated_at = "", "", "", now
                recovered += 1
        return recovered

    def recover(self):
        from .discovery_sessions import DiscoverySessions

        coordinator = DiscoverySessions(self)
        with self.sessions() as session:
            has_batches = coordinator.has_recovery(session)
        return self._recover_legacy() + (coordinator.recover() if has_batches else 0)

    def _owned(self, session, candidate_id, owner):
        row = session.get(DiscoveryCandidate, candidate_id)
        if row is None or row.owner != owner or row.status != "reserved" or row.lease_until <= now_iso():
            return None
        return row

    def _reserve(self, candidate_id):
        with self._transaction() as session:
            row = session.get(DiscoveryCandidate, candidate_id)
            if row is None or not self._eligible(session, row, now_iso()) or row.status == "accepted":
                return None
            if self.provider_config.kind == "extractive":
                row.status, row.error_code = "needs_attention", "provider_unsupported"
                return None
            prompt = decision_prompt(row.id, row.payload, row.latest_metrics)
            if len(prompt) > MAX_PROMPT_CHARS:
                row.status, row.error_code = "needs_attention", "prompt_too_large"
                return None
            owner = str(uuid4())
            identity = {"kind": self.provider_config.kind, "model": self.provider_config.model,
                "config_fingerprint": _hash(self.provider_config.model_dump(mode="json")),
                "request_upper_bound": 3 if self.provider_config.kind in {"openai", "openai_chat", "anthropic"} else None}
            row.status, row.owner, row.provider_identity = "reserved", owner, identity
            row.retry_at, row.error_code = "", ""
            row.lease_until = (datetime.now(UTC) + timedelta(seconds=self.provider_config.timeout_seconds + 60)).isoformat()
            row.updated_at = now_iso()
            call = DiscoveryCall(candidate_id=row.id, fingerprint=row.fingerprint, owner=owner, provider=identity)
            session.add(call)
            session.flush()
            return row.id, owner, call.id, row.payload, prompt

    def _failure(self, candidate_id, owner, call_id, exc, *, format_invalid=False):
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        if format_invalid:
            state, code = "needs_attention", "format_invalid"
        elif status == 402:
            state, code = "needs_attention", "insufficient_balance"
        elif status in (401, 403):
            state, code = "needs_attention", "auth_required"
        elif status == 429 or isinstance(status, int) and 500 <= status <= 599:
            state, code = "retry_wait", "rate_limited" if status == 429 else "upstream_error"
        else:
            state, code = "unknown", "outcome_unknown"
        with self._transaction() as session:
            row = self._owned(session, candidate_id, owner)
            if row is None:
                return
            if state == "retry_wait" and self._calls(session, row.id) >= min(2, self.settings.max_calls_per_candidate):
                state, code = "needs_attention", "call_budget_exhausted"
            row.status, row.error_code, row.updated_at = state, code, now_iso()
            row.lease_until = ""
            row.retry_at = (datetime.now(UTC) + timedelta(seconds=RETRY_SECONDS)).isoformat() if state == "retry_wait" else ""
            call = session.get(DiscoveryCall, call_id)
            call.status, call.error_code, call.completed_at = "failed" if state != "unknown" else "unknown", code, now_iso()

    def _save(self, candidate_id, owner, call_id, result):
        with self._transaction() as session:
            row = self._owned(session, candidate_id, owner)
            if row is None:
                return False
            value = result.model_dump(mode="json")
            call = session.get(DiscoveryCall, call_id)
            call.status, call.result, call.completed_at = "completed", deepcopy(value), now_iso()
            row.result, row.judged_at, row.updated_at, row.lease_until = value, now_iso(), now_iso(), ""
            row.status = "accepted" if self._latest(session, row) else "superseded"
            return row.status == "accepted"

    def _apply(self, candidate_id):
        try:
            with self._transaction() as session:
                row = session.get(DiscoveryCandidate, candidate_id)
                if row is None or row.status != "accepted" or not self._eligible(session, row, now_iso()):
                    return False
                frozen = _hash([row.payload, row.result, row.fingerprint, row.policy, row.judged_at])
                self.publish_callback(session, row)
                if frozen != _hash([row.payload, row.result, row.fingerprint, row.policy, row.judged_at]):
                    raise ValueError("Discovery application must preserve frozen evidence and decision")
                row.status, row.applied_at, row.updated_at = "done", now_iso(), now_iso()
                row.error_code, row.retry_at = "", ""
            return True
        except Exception:
            with self._transaction() as session:
                row = session.get(DiscoveryCandidate, candidate_id)
                if row and row.status == "accepted":
                    row.error_code, row.updated_at = "apply_failed", now_iso()
                    row.retry_at = (datetime.now(UTC) + timedelta(seconds=RETRY_SECONDS)).isoformat()
            return False

    async def pending(self, limit=None):
        result = {"processed": 0, "judged": 0, "applied": 0, "failed": 0}
        if not self.settings.enabled:
            return {**result, "more_pending": False}
        self.recover()
        if self.settings.session_reuse and self.provider_config.kind == "codex":
            from .discovery_sessions import DiscoverySessions

            return await DiscoverySessions(self).pending(limit)
        limit = min(2, self.settings.batch_size, limit if limit is not None else self.settings.batch_size)
        with self.sessions() as session:
            rows = session.scalars(select(DiscoveryCandidate).where(
                DiscoveryCandidate.status.in_(("accepted", "pending", "retry_wait")),
            ).order_by(DiscoveryCandidate.created_at, DiscoveryCandidate.id))
            keys = []
            for row in self.ordered(rows):
                if len(keys) >= max(0, limit):
                    break
                if self._eligible(session, row, now_iso()):
                    keys.append((row.id, row.status))
        for key, state in keys:
            result["processed"] += 1
            if state == "accepted":
                applied = self._apply(key)
                result["applied"] += int(applied)
                result["failed"] += int(not applied)
                continue
            reservation = self._reserve(key)
            if reservation is None:
                continue
            candidate_id, owner, call_id, payload, prompt = reservation
            try:
                with usage.scope("discovery_foresight"):
                    async with asyncio.timeout(self.provider_config.timeout_seconds):
                        response = await make_provider(self.provider_config).complete(prompt, DiscoveryDecision)
            except BaseException as exc:
                self._failure(candidate_id, owner, call_id, exc)
                result["failed"] += 1
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                continue
            try:
                decision = validate_decision(response, candidate_id, payload)
            except (ValueError, TypeError, KeyError) as exc:
                self._failure(candidate_id, owner, call_id, exc, format_invalid=True)
                result["failed"] += 1
                continue
            if self._save(candidate_id, owner, call_id, decision):
                result["judged"] += 1
                applied = self._apply(candidate_id)
                result["applied"] += int(applied)
                result["failed"] += int(not applied)
        return {**result, "more_pending": self.has_pending()}

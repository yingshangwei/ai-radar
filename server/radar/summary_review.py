"""Durable, bounded fact review. Only exact approved candidates leave this service."""

import asyncio
import hashlib
import json
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import anthropic
import httpx
import openai
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import RadarConfig
from .models import SummaryReview, now_iso
from .providers import (
    READING_INSTRUCTIONS,
    make_provider,
    prompt_for,
    validate_reading_result,
    validate_result,
)
from .schemas import DigestOutput, ReadingOutput
from .summary_contracts import (
    SUMMARY_REVIEW_POLICY,
    SummaryAuditOutput,
    SummaryCorrections,
    SummaryUnit,
    UnitAudit,
    audit_prompt,
    correction_prompt,
    digest_units,
    document_units,
    validate_audit,
    validate_corrections,
)
from .summary_evidence import freeze_review_evidence, review_evidence_fingerprint

WORKFLOW_VERSION = "durable-summary-review-v1"
TRANSPORT_BACKOFF = (60, 300)
RETRYABLE_TRANSPORT = frozenset({"provider_timeout", "provider_connection", "provider_rate_limited", "provider_server_error"})
FORMAT_FEEDBACK = "前次响应未满足结构协议。仅按原 JSON_SCHEMA 返回完整 JSON，不改变审核标准。\n"


class SummaryReviewPending(ValueError):
    def __init__(self, message: str = "摘要事实审核尚未通过，已保存处理进度，请稍后查看。"):
        super().__init__(message)


class SummaryReviewYield(SummaryReviewPending):
    """An intentional pause between saved calls, ready for the next queue batch."""


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _snapshot(row: SummaryReview) -> dict:
    return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


def _failure(exc: BaseException) -> tuple[str, str]:
    # Only fixed diagnostics: never exception text, response bodies or credentials.
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return "call_cancelled", "CancelledError"
    if isinstance(exc, (TimeoutError, openai.APITimeoutError, anthropic.APITimeoutError, httpx.TimeoutException)):
        return "provider_timeout", "TimeoutError"
    if isinstance(exc, (openai.APIConnectionError, anthropic.APIConnectionError, httpx.TransportError, ConnectionError)):
        return "provider_connection", "ConnectionError"
    if isinstance(exc, (openai.APIStatusError, anthropic.APIStatusError, httpx.HTTPStatusError)):
        status = exc.response.status_code
        if status == 429:
            return "provider_rate_limited", "HTTPStatusError"
        if 500 <= status <= 599:
            return "provider_server_error", "HTTPStatusError"
        return "provider_http_error", "HTTPStatusError"
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return "format_invalid", "ValidationError" if isinstance(exc, ValidationError) else "ValueError"
    return "provider_failure", "provider_error"


class SummaryReviewService:
    def __init__(self, sessions, config: RadarConfig):
        self.sessions = sessions
        self.stage_call_limit = None
        self._slice = ContextVar("summary_review_slice", default=None)
        self.config = config.model_copy(deep=True)
        self.review = self.config.summary_review
        self.audit_config = self.review.provider or self.config.provider
        self.config_fingerprint = _hash({
            "generator": self.config.provider.model_dump(), "review": self.review.model_dump(),
            "workflow": WORKFLOW_VERSION, "policy": SUMMARY_REVIEW_POLICY,
        })

    def _identity(self, kind, scope, sources, evidence, date=""):
        if not isinstance(scope, str) or not 1 <= len(scope) <= 240:
            raise SummaryReviewPending()
        try:
            frozen = freeze_review_evidence(evidence if evidence is not None else sources)
            ids = [source["id"] for source in sources]
            if not ids or len(set(ids)) != len(ids) or set(ids) != {source["id"] for source in frozen}:
                raise ValueError("Evidence scope mismatch")
            fingerprint = review_evidence_fingerprint(frozen)
            key = _hash([kind, scope, date, fingerprint, self.config_fingerprint, SUMMARY_REVIEW_POLICY])
        except (ValueError, TypeError, KeyError):
            raise SummaryReviewPending() from None
        return key, frozen, fingerprint

    @contextmanager
    def _transaction(self):
        with self.sessions.begin() as session:
            if session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session

    @staticmethod
    def _owned(session, key, owner):
        row = session.scalar(select(SummaryReview).where(
            SummaryReview.id == key, SummaryReview.owner == owner, SummaryReview.lease_until > now_iso(),
        ).with_for_update())
        if row is None:
            raise SummaryReviewPending()
        return row

    def _claim(self, key, kind, scope, sources, frozen, fingerprint):
        owner = str(uuid4())
        for attempt in range(2):
            try:
                with self._transaction() as session:
                    row = session.scalar(select(SummaryReview).where(SummaryReview.id == key).with_for_update())
                    if row is None:
                        row = SummaryReview(
                            id=key, scope=scope, kind=kind, policy=SUMMARY_REVIEW_POLICY,
                            config_fingerprint=self.config_fingerprint, evidence_fingerprint=fingerprint,
                            evidence=frozen, generator_sources=json.loads(json.dumps(sources, allow_nan=False)),
                        )
                        session.add(row)
                        session.flush()
                    if not self._context_valid(_snapshot(row)):
                        raise SummaryReviewPending()
                    if row.status == "ready":
                        return _snapshot(row), None
                    if (row.status != "pending" or row.lease_until > now_iso()
                            or row.retry_at > now_iso()):
                        raise SummaryReviewPending()
                    row.owner = owner
                    row.lease_until = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
                    row.updated_at = now_iso()
                    return _snapshot(row), owner
            except IntegrityError:
                if attempt:
                    raise SummaryReviewPending() from None
            except (ValueError, TypeError, KeyError):
                raise SummaryReviewPending() from None
        raise SummaryReviewPending()

    def _read(self, key, owner):
        with self.sessions() as session:
            return _snapshot(self._owned(session, key, owner))

    def _blocked(self, key, owner, code, *, semantic=False):
        with self._transaction() as session:
            row = self._owned(session, key, owner)
            row.status = "review_required" if semantic else "error"
            row.failure_code = code
            row.history = row.history + [{"event": "blocked", "code": code, "at": now_iso()}]
            row.updated_at = now_iso()
        raise SummaryReviewPending()

    @staticmethod
    def _units(row):
        if row["kind"] == "digest":
            return digest_units(DigestOutput.model_validate(row["candidate"]), row["evidence"])
        return document_units(ReadingOutput.model_validate(row["candidate"]), row["evidence"])

    @staticmethod
    def _unit_fingerprint(row, unit):
        return _hash([unit.model_dump(), row["evidence_fingerprint"], row["config_fingerprint"],
                      SUMMARY_REVIEW_POLICY])

    def _receipt(self, row, unit):
        fingerprint = self._unit_fingerprint(row, unit)
        for event in reversed(row["history"]):
            if event.get("event") == "audit" and event.get("unit_id") == unit.unit_id \
                    and event.get("fingerprint") == fingerprint:
                output = SummaryAuditOutput(audits=[UnitAudit.model_validate(event["audit"])])
                return validate_audit(output.model_dump_json(), [unit]).audits[0]
        return None

    def _context_valid(self, row):
        return (row["policy"] == SUMMARY_REVIEW_POLICY
                and row["config_fingerprint"] == self.config_fingerprint
                and review_evidence_fingerprint(row["evidence"]) == row["evidence_fingerprint"])

    def _approved(self, row):
        if not self._context_valid(row):
            return False
        units = self._units(row)
        return all((receipt := self._receipt(row, unit)) is not None and receipt.passed for unit in units)

    @staticmethod
    def _attempts(state, target):
        calls = [event for event in state["history"] if event.get("event") == "call" and event["target"] == target]
        outcomes = {event["call_id"]: event for event in state["history"]
                    if event.get("call_id") and event.get("event") != "call"}
        failures = [outcomes[event["call_id"]] for event in calls if event["call_id"] in outcomes]
        formats = sum(event.get("code") == "format_invalid" for event in failures)
        transports = sum(event.get("code") in RETRYABLE_TRANSPORT for event in failures)
        return calls, outcomes, formats, transports

    def _budget_block(self, state, stage, target, unit_id):
        if sum(state["call_counts"].get(name, 0) for name in ("generation", "audit", "correction")) >= self.review.max_calls:
            return "global_call_budget_exhausted"
        calls, outcomes, formats, transports = self._attempts(state, target)
        if calls:
            outcome = outcomes.get(calls[-1]["call_id"])
            if outcome is None:
                return "unknown_call_outcome"
            if outcome.get("event") != "failure":
                return "unchanged_call_not_repeated"
            code = outcome.get("code")
            if code == "format_invalid":
                if formats > self.review.max_format_retries:
                    return "format_budget_exhausted"
            elif code in RETRYABLE_TRANSPORT:
                if transports > len(TRANSPORT_BACKOFF):
                    return "transport_budget_exhausted"
            else:
                return "unchanged_call_not_repeated"
            if len(calls) >= 1 + self.review.max_format_retries + len(TRANSPORT_BACKOFF):
                return "call_budget_exhausted"
        elif stage == "correction" and state["correction_rounds"].get(unit_id, 0) >= self.review.max_correction_rounds:
            return "correction_budget_exhausted"
        return ""

    def _next_call(self, state):
        if not state["candidate"]:
            return "generation", _hash([state["id"], "generation"]), ""
        for unit in self._units(state):
            receipt = self._receipt(state, unit)
            if receipt is not None and receipt.passed:
                continue
            stage = "audit" if receipt is None else "correction"
            return stage, _hash([stage, self._unit_fingerprint(state, unit)]), unit.unit_id
        return None

    def can_analyze_documents(self, scope, sources, *, evidence=None) -> bool:
        if not self.review.enabled:
            return False
        try:
            key, _, _ = self._identity("documents", scope, sources, evidence)
            with self.sessions() as session:
                row = session.get(SummaryReview, key)
                if row is None:
                    return True
                state = _snapshot(row)
            if not self._context_valid(state):
                return False
            if state["status"] == "ready":
                return self._approved(state)
            if state["status"] != "pending" or state["lease_until"] > now_iso() or state["retry_at"] > now_iso():
                return False
            planned = self._next_call(state)
            return planned is None or not self._budget_block(state, *planned)
        except (SummaryReviewPending, ValueError, TypeError, KeyError):
            return False

    def _reserve(self, key, owner, stage, target, timeout, unit_id, prompt):
        blocked = ""
        with self._transaction() as session:
            row = self._owned(session, key, owner)
            state = _snapshot(row)
            if row.retry_at > now_iso():
                raise SummaryReviewPending()
            calls, _, formats, _ = self._attempts(state, target)
            blocked = self._budget_block(state, stage, target, unit_id)
            actual_prompt = (FORMAT_FEEDBACK if formats else "") + prompt
            if not blocked and len(actual_prompt) > self.review.max_prompt_chars:
                blocked = "prompt_too_large"
            if blocked:
                row.status = "review_required" if blocked == "correction_budget_exhausted" else "error"
                row.failure_code = blocked
                row.history = row.history + [{"event": "blocked", "code": blocked, "at": now_iso()}]
                row.updated_at = now_iso()
            else:
                if not calls and stage == "correction":
                    row.correction_rounds = {**row.correction_rounds, unit_id: row.correction_rounds.get(unit_id, 0) + 1}
                call_id = str(uuid4())
                provider_config = self.config.provider if stage == "generation" else self.audit_config
                # The supported SDKs have max_retries=2. CLI internal requests are unknown.
                request_bound = 3 if provider_config.kind in {"openai", "openai_chat", "anthropic"} else None
                previous_bound = row.call_counts.get("request_upper_bound", 0)
                total_bound = None if request_bound is None or previous_bound is None else previous_bound + request_bound
                row.call_counts = {**row.call_counts, stage: row.call_counts.get(stage, 0) + 1,
                    "request_upper_bound": total_bound}
                row.retry_at = ""
                row.lease_until = (datetime.now(UTC) + timedelta(seconds=timeout + 60)).isoformat()
                row.updated_at = now_iso()
                row.history = row.history + [{"event": "call", "call_id": call_id, "stage": stage,
                    "target": target, "unit_id": unit_id, "attempt": len(calls) + 1,
                    "request_upper_bound": request_bound, "at": now_iso()}]
        if blocked:
            raise SummaryReviewPending()
        return call_id, actual_prompt

    async def _call(self, key, owner, *, stage, target, unit_id="", prompt, timeout,
                    invoke: Callable, validate: Callable, save: Callable):
        while True:
            budget = self._slice.get()
            if budget is not None:
                if budget[0] <= 0:
                    code = self._budget_block(self._read(key, owner), stage, target, unit_id)
                    if code:
                        self._blocked(key, owner, code, semantic=code == "correction_budget_exhausted")
                    raise SummaryReviewYield()
                budget[0] -= 1
            call_id, actual_prompt = self._reserve(key, owner, stage, target, timeout, unit_id, prompt)
            try:
                async with asyncio.timeout(timeout):
                    response = await invoke(actual_prompt)
                parsed = validate(response)
            except BaseException as exc:
                code, exception_type = _failure(exc)
                with self._transaction() as session:
                    row = self._owned(session, key, owner)
                    row.history = row.history + [{"event": "failure", "call_id": call_id, "stage": stage,
                        "code": code, "exception_type": exception_type, "at": now_iso()}]
                    row.failure_code = code
                    if code in RETRYABLE_TRANSPORT:
                        _, _, _, transports = self._attempts(_snapshot(row), target)
                        if transports <= len(TRANSPORT_BACKOFF):
                            row.retry_at = (datetime.now(UTC) + timedelta(seconds=TRANSPORT_BACKOFF[transports - 1])).isoformat()
                        else:
                            row.status, row.failure_code = "error", "transport_budget_exhausted"
                    elif code != "format_invalid":
                        row.status = "error"
                    row.updated_at = now_iso()
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                if code == "format_invalid":
                    continue
                raise SummaryReviewPending() from None
            with self._transaction() as session:
                row = self._owned(session, key, owner)
                row.failure_code, row.retry_at = "", ""
                save(row, parsed, call_id)
                row.updated_at = now_iso()
            return

    async def generate_digest(self, scope: str, sources: list[dict], date: str, provider,
                              *, evidence: list[dict] | None = None) -> DigestOutput:
        return await self._run("digest", scope, sources, provider, evidence, date)

    async def analyze_documents(self, scope: str, sources: list[dict], provider,
                                *, evidence: list[dict] | None = None) -> ReadingOutput:
        return await self._run("documents", scope, sources, provider, evidence)

    async def _run(self, kind, scope, sources, provider, evidence, date=""):
        token = self._slice.set(None if self.stage_call_limit is None else [self.stage_call_limit])
        try:
            return await self._run_claimed(kind, scope, sources, provider, evidence, date)
        finally:
            self._slice.reset(token)

    async def _run_claimed(self, kind, scope, sources, provider, evidence, date=""):
        if not self.review.enabled:
            raise SummaryReviewPending()
        key, frozen, fingerprint = self._identity(kind, scope, sources, evidence, date)
        state, owner = self._claim(key, kind, scope, sources, frozen, fingerprint)
        schema = DigestOutput if kind == "digest" else ReadingOutput
        if owner is None:
            try:
                if self._approved(state):
                    return schema.model_validate(state["candidate"])
            except (ValueError, TypeError, KeyError):
                pass
            raise SummaryReviewPending()
        try:
            if not state["candidate"]:
                inputs = state["generator_sources"]
                if kind == "digest":
                    prompt = prompt_for(inputs, date)
                else:
                    prompt = (READING_INSTRUCTIONS + "\nJSON_SCHEMA:\n"
                              + json.dumps(ReadingOutput.model_json_schema(), ensure_ascii=False)
                              + "\nUNTRUSTED_DOCUMENTS:\n" + json.dumps(inputs, ensure_ascii=False))

                async def generate(actual_prompt):
                    return await provider.complete(actual_prompt, schema)

                def validate_candidate(value):
                    parsed = validate_result(value, inputs) if kind == "digest" else validate_reading_result(value, inputs)
                    digest_units(parsed, frozen) if kind == "digest" else document_units(parsed, frozen)
                    return parsed

                def save_candidate(row, value, call_id):
                    row.candidate = value.model_dump()
                    row.history = row.history + [{"event": "candidate", "stage": "generation", "call_id": call_id,
                                                  "candidate": row.candidate, "at": now_iso()}]

                await self._call(key, owner, stage="generation", target=_hash([key, "generation"]),
                    prompt=prompt, timeout=self.config.provider.timeout_seconds, invoke=generate,
                    validate=validate_candidate, save=save_candidate)

            audit_provider = None

            async def audit(actual_prompt):
                nonlocal audit_provider
                if audit_provider is None:
                    audit_provider = make_provider(self.audit_config)
                return await audit_provider.complete(actual_prompt, SummaryAuditOutput)

            async def correct(actual_prompt):
                nonlocal audit_provider
                if audit_provider is None:
                    audit_provider = make_provider(self.audit_config)
                return await audit_provider.complete(actual_prompt, SummaryCorrections)

            while True:
                state = self._read(key, owner)
                units = self._units(state)
                unit = next((unit for unit in units if (receipt := self._receipt(state, unit)) is None
                             or not receipt.passed), None)
                if unit is None:
                    with self._transaction() as session:
                        row = self._owned(session, key, owner)
                        if not self._approved(_snapshot(row)):
                            raise SummaryReviewPending()
                        row.status, row.failure_code, row.updated_at = "ready", "", now_iso()
                        result = schema.model_validate(row.candidate)
                    return result
                receipt = self._receipt(state, unit)
                unit_fingerprint = self._unit_fingerprint(state, unit)
                if receipt is None:
                    prompt = audit_prompt([unit], state["evidence"])

                    def save_audit(row, output, call_id, unit=unit, unit_fingerprint=unit_fingerprint):
                        row.history = row.history + [{"event": "audit", "call_id": call_id,
                            "unit_id": unit.unit_id, "fingerprint": unit_fingerprint,
                            "audit": output.audits[0].model_dump(), "at": now_iso()}]

                    await self._call(key, owner, stage="audit", target=_hash(["audit", unit_fingerprint]),
                        unit_id=unit.unit_id, prompt=prompt, timeout=self.audit_config.timeout_seconds,
                        invoke=audit, validate=lambda value, unit=unit: validate_audit(value, [unit]), save=save_audit)
                else:
                    prompt = correction_prompt([unit], state["evidence"], SummaryAuditOutput(audits=[receipt]))

                    def save_correction(row, corrected, call_id, unit=unit):
                        replacement = corrected[0]
                        previous = self._unit_fingerprint(_snapshot(row), unit)
                        current = self._units(_snapshot(row))
                        new_units = [replacement if item.unit_id == unit.unit_id else item for item in current]
                        row.candidate = self._assemble(kind, new_units)
                        unchanged = previous == self._unit_fingerprint(_snapshot(row), replacement)
                        row.history = row.history + [{"event": "candidate", "stage": "correction",
                            "call_id": call_id, "unit_id": unit.unit_id, "candidate": replacement.candidate,
                            "before_fingerprint": previous, "at": now_iso()}]
                        if unchanged:
                            row.status, row.failure_code = "review_required", "correction_unchanged"

                    await self._call(key, owner, stage="correction", target=_hash(["correction", unit_fingerprint]),
                        unit_id=unit.unit_id, prompt=prompt, timeout=self.audit_config.timeout_seconds,
                        invoke=correct, validate=lambda value, unit=unit: validate_corrections(value, [unit]), save=save_correction)
                    if self._read(key, owner)["status"] != "pending":
                        raise SummaryReviewPending()
        except SummaryReviewPending:
            raise
        except (ValueError, TypeError, KeyError):
            self._blocked(key, owner, "invalid_saved_state")
        finally:
            with self._transaction() as session:
                row = session.scalar(select(SummaryReview).where(
                    SummaryReview.id == key, SummaryReview.owner == owner,
                ).with_for_update())
                if row is not None:
                    row.owner, row.lease_until = "", ""

    @staticmethod
    def _assemble(kind, units: list[SummaryUnit]) -> dict:
        if kind == "documents":
            return ReadingOutput.model_validate({"documents": [unit.candidate for unit in units]}).model_dump()
        header = next(unit for unit in units if unit.kind == "header")
        return DigestOutput.model_validate({**header.candidate,
            "stories": [unit.candidate for unit in units if unit.kind == "story"]}).model_dump()

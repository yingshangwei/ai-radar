"""Content-free usage receipts, independent of model providers and business state.

Only provider-reported integers are counted. Cache and reasoning are subsets;
unknown receipts remain visible. Replaying a durable CLI receipt is idempotent.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)
_scope = ContextVar("model_usage_scope", default=("other", "generation"))
_failures = 0
FEATURES = {
    "translation": "普通翻译", "technical_translation": "技术翻译",
    "digest": "每日汇报", "web_reading": "网页解读", "radar_heading": "雷达标题",
    "discovery_foresight": "动态关注与前瞻预判", "other": "其他调用",
}
STAGES = {"draft": "初稿", "generation": "生成", "correction": "校对", "audit": "独立审计"}
for _stage, _label in list(STAGES.items()):
    for _role, _role_label in {"standard": "常规处理", "confirmation": "前置确认", "adjudication": "疑难决断（按需）"}.items():
        for _effort in ("low", "medium"):
            STAGES[f"{_stage}_{_role}_{_effort}"] = f"{_label} · {_role_label} · {_effort}"
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens")


@contextmanager
def scope(feature, stage="generation", *, default_only=False):
    value = _scope.get() if default_only and _scope.get()[0] != "other" else (feature, stage)
    token = _scope.set(value)
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope():
    return _scope.get()


def vendor(base_url, fallback="openai"):
    host = urlparse(str(base_url or "")).hostname or ""
    if host.endswith("aliyuncs.com"):
        return "bailian"
    if host == "api.deepseek.com":
        return "deepseek"
    return fallback if not host or host == "api.openai.com" else "openai_compatible"


def label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,100}", value) else None


def count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def normalize(raw, provider):
    raw = raw if isinstance(raw, dict) else {}
    inp = count(raw.get("input_tokens", raw.get("prompt_tokens")))
    out = count(raw.get("output_tokens", raw.get("completion_tokens")))
    prompt = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
    prompt = prompt if isinstance(prompt, dict) else {}
    completion = completion if isinstance(completion, dict) else {}
    cached = count(raw.get("cached_input_tokens", raw.get("cache_read_input_tokens",
        raw.get("prompt_cache_hit_tokens", prompt.get("cached_tokens")))))
    written = count(raw.get("cache_creation_input_tokens"))
    reasoning = count(raw.get("reasoning_output_tokens", completion.get("reasoning_tokens")))
    # Anthropic input_tokens excludes cache reads/writes. OpenAI/Codex includes them.
    if provider in {"anthropic", "claude_cli"} and inp is not None:
        inp += (cached or 0) + (written or 0)
    return dict(zip(TOKEN_FIELDS, (inp, out, cached, written, reasoning), strict=True))


class UsageStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS usage_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS usage_calls(
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    provider TEXT NOT NULL, model TEXT NOT NULL, requested_model TEXT,
                    model_basis TEXT NOT NULL, feature TEXT NOT NULL, stage TEXT NOT NULL,
                    outcome TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
                    cached_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
                    http_status INTEGER
                );
                CREATE INDEX IF NOT EXISTS usage_started ON usage_calls(started_at);
            """)
            db.execute("INSERT OR IGNORE INTO usage_meta VALUES('tracking_started_at', ?)", (now(),))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def write(self, row):
        keys = list(row)
        with self.connect() as db:
            db.execute(f"INSERT INTO usage_calls ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)}) "
                "ON CONFLICT(id) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in keys if k != "id")
                + " WHERE usage_calls.finished_at IS NULL", list(row.values()))

    def recover(self, key, raw):
        values = normalize(raw, "codex")
        if values["input_tokens"] is None or values["output_tokens"] is None:
            return
        with self.connect() as db:
            # Only existing reservations: historical calls with no function label
            # must never be invented or attributed to the current configuration.
            db.execute("UPDATE usage_calls SET " + ",".join(f"{k}=?" for k in values)
                + ",finished_at=?,outcome='completed' WHERE id=? AND input_tokens IS NULL",
                [*values.values(), now(), key])

    def report(self, period="7d", timezone="Asia/Shanghai"):
        zone = ZoneInfo(timezone)
        current = datetime.now(UTC)
        midnight = current.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        since = (midnight - timedelta(days={"today": 0, "7d": 6, "30d": 29}.get(period, 0))).astimezone(UTC)
        start = "" if period == "all" else since.isoformat()
        sums = ",".join(f"SUM({key}) AS {key}" for key in TOKEN_FIELDS)
        aggregates = f"""COUNT(*) AS calls, {sums},
            SUM(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN 1 ELSE 0 END) AS reported_calls,
            SUM(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN input_tokens+output_tokens END) AS total_tokens,
            SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) AS error_calls,
            SUM(CASE WHEN outcome='started' AND started_at >= ? THEN 1 ELSE 0 END) AS active_calls"""
        stale = (current - timedelta(minutes=20)).isoformat()
        with self.connect() as db:
            tracked = db.execute("SELECT value FROM usage_meta WHERE key='tracking_started_at'").fetchone()[0]
            rows = [dict(r) for r in db.execute(f"""SELECT provider,model,feature,stage,{aggregates}
                FROM usage_calls WHERE started_at >= ? GROUP BY provider,model,feature,stage
                ORDER BY total_tokens DESC,provider,model,feature,stage""", (stale, start))]
            totals = dict(db.execute(f"SELECT {aggregates} FROM usage_calls WHERE started_at >= ?",
                (stale, start)).fetchone())
            recent = [dict(r) for r in db.execute("""SELECT * FROM usage_calls WHERE started_at >= ?
                ORDER BY started_at DESC,id DESC LIMIT 50""", (start,))]
        for row in [totals, *rows]:
            row["active_calls"] = row["active_calls"] or 0
            row["reported_calls"] = row["reported_calls"] or 0
            row["error_calls"] = row["error_calls"] or 0
            row["unknown_calls"] = row["calls"] - row["reported_calls"] - row["active_calls"]
            if row["calls"] == 0:
                row["total_tokens"] = 0
        for row in recent:
            row["total_tokens"] = (row["input_tokens"] + row["output_tokens"]
                if row["input_tokens"] is not None and row["output_tokens"] is not None else None)
            if row["outcome"] == "started" and row["started_at"] < stale:
                row["outcome"] = "unknown"
        return {"enabled": True, "available": True, "period": period, "timezone": timezone,
            "tracking_started_at": tracked, "as_of": current.isoformat(), "since": start or tracked,
            "totals": totals, "groups": rows, "recent": recent, "recording_errors": _failures}


def now():
    return datetime.now(UTC).isoformat()


@lru_cache(maxsize=4)
def _store(path):
    return UsageStore(path)


def store(path=None):
    path = os.environ.get("RADAR_USAGE_DATABASE_PATH", "") if path is None else path
    return _store(path) if path else None


def _failed():
    global _failures
    _failures += 1
    logger.warning("Usage receipt could not be persisted; no model request is retried by metering")


class Call:
    def __init__(self, provider, model=None, *, key=None):
        feature, stage = _scope.get()
        self.row = {"id": key or str(uuid4()), "started_at": now(), "finished_at": None,
            "provider": label(provider) or "unknown", "model": label(model) or "auto-unreported",
            "requested_model": label(model), "model_basis": "configured" if label(model) else "unknown",
            "feature": feature if feature in FEATURES else "other",
            "stage": stage if stage in STAGES else "generation", "outcome": "started",
            **dict.fromkeys(TOKEN_FIELDS), "http_status": None}
        self.save()

    def save(self):
        try:
            target = store()
            if target:
                target.write(self.row)
        except (OSError, sqlite3.Error):
            _failed()

    def finish(self, raw=None, *, model=None, outcome="completed", http_status=None):
        self.row.update(normalize(raw, self.row["provider"]), finished_at=now(), outcome=outcome,
            http_status=http_status)
        if label(model):
            self.row.update(model=model, model_basis="reported")
        self.save()


class UsageHTTPClient(httpx.AsyncClient):
    """HTTPX hooks see every physical SDK attempt, including built-in retries."""

    def __init__(self, provider, model, timeout):
        self.provider, self.model = provider, model
        self.pending = {}
        super().__init__(timeout=timeout, follow_redirects=True,
            event_hooks={"request": [self._request], "response": [self._response]})

    async def _request(self, request):
        call = Call(self.provider, self.model)
        request.extensions["radar_usage_id"] = call.row["id"]
        self.pending[call.row["id"]] = call

    async def _response(self, response):
        # Current model adapters use non-streaming API responses only.
        await response.aread()
        call = self.pending.pop(response.request.extensions.get("radar_usage_id"), None)
        if call is None:
            return
        try:
            data = response.json() if len(response.content) <= 8_000_000 else {}
            data = data if isinstance(data, dict) else {}
        except (ValueError, UnicodeError):
            data = {}
        call.finish(data.get("usage"), model=data.get("model"),
            outcome="completed" if response.is_success else "error", http_status=response.status_code)

    async def aclose(self):
        try:
            await super().aclose()
        finally:
            self._finish_pending()

    async def __aexit__(self, *args):
        try:
            return await super().__aexit__(*args)
        finally:
            self._finish_pending()

    def _finish_pending(self):
        for call in self.pending.values():
            call.finish(outcome="unknown")
        self.pending.clear()


def codex_receipt(stdout, stderr=b""):
    totals, model = {}, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        model = label(event.get("model")) or model
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"):
                value = count(event["usage"].get(key))
                if value is not None:
                    totals[key] = totals.get(key, 0) + value
    # Plain CLI startup diagnostics may report the selected model. Never retain stderr.
    match = re.search(rb"(?m)^model: ([A-Za-z0-9_./:-]{1,100})\s*$", stderr)
    return totals, model or (match[1].decode() if match else None)


def session_key(root, fingerprint):
    return "codex-" + hashlib.sha256((str(root) + ":" + fingerprint).encode()).hexdigest()


def recover_session(root, fingerprint, raw):
    try:
        target = store()
        if target:
            target.recover(session_key(root, fingerprint), raw)
    except (OSError, sqlite3.Error):
        _failed()


def allocation(config):
    from .config import provider_model

    result = []

    def add(feature, stage, provider, model):
        result.append({"feature": feature, "stage": stage, "provider": provider,
            "model": label(model) or "auto-unreported"})

    def engine(feature, stage, provider):
        from . import model_router

        if model_router.active(provider):
            role = model_router.role_for(feature, stage)
            profile = getattr(model_router.config(), role)
            add(feature, model_router.stage_for(stage, role, profile), "codex", profile.model)
            if role == "confirmation":
                final = model_router.config().adjudication
                add(feature, model_router.stage_for(stage, "adjudication", final), "codex", final.model)
            return
        add(feature, stage, vendor(provider.base_url, provider.kind)
            if provider.kind in {"openai", "openai_chat"} else provider.kind, provider_model(provider))

    tr = config.translation
    if tr.enabled:
        for feature in ("translation", "technical_translation"):
            add(feature, "draft", vendor(tr.base_url), tr.model)
            for stage, model in (("correction", tr.review_model), ("audit", tr.audit_model or tr.review_model)):
                if feature == "technical_translation" and tr.technical_review_provider:
                    engine(feature, stage, tr.technical_review_provider)
                else:
                    add(feature, stage, vendor(tr.base_url), model)
    for feature in ("digest", "radar_heading", "web_reading"):
        engine(feature, "generation", config.provider)
        if config.summary_review.enabled:
            for stage in ("correction", "audit"):
                engine(feature, stage, config.summary_review.provider or config.provider)
    if config.discovery.enabled:
        engine("discovery_foresight", "generation", config.discovery.provider or config.provider)
    return result

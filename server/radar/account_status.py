"""Read-only vendor account queries, independent of model/content workflows."""

import asyncio
import hashlib
import json
import math
import os
import signal
import sqlite3
import sys
import tempfile
import time
import tomllib
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import secret

MAX_RESPONSE = 256_000
ERRORS = {
    "authorization_required": "需要配置或更新此账户的查询授权。",
    "query_failed": "本次查询失败，暂时无法确认当前余额或额度。",
    "invalid_response": "厂商返回的数据不完整，暂时无法确认当前余额或额度。",
    "unsupported": "厂商未提供已接入的官方余额查询接口。",
    "not_checked": "正在等待首次查询。",
}


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    provider: Literal["deepseek", "bailian", "codex", "openai", "anthropic"]
    name: str = Field(max_length=80)
    in_use: bool = True
    api_key_env: str = "DEEPSEEK_API_KEY"
    access_key_id_env: str = "ALIBABA_CLOUD_ACCESS_KEY_ID"
    access_key_secret_env: str = "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
    security_token_env: str = "ALIBABA_CLOUD_SECURITY_TOKEN"
    credentials_file: str | None = None
    codex_command: list[str] = Field(default_factory=lambda: ["codex"], min_length=1, max_length=8)
    codex_home: str | None = None
    billing_python: str | None = None
    low_balance: dict[str, float] = Field(default_factory=lambda: {"CNY": 10, "USD": 2})
    low_remaining_percent: float = Field(default=10, ge=0, le=100)

    @field_validator("low_balance")
    @classmethod
    def thresholds(cls, value):
        if any(k not in {"CNY", "USD", "JPY"} or not math.isfinite(v) or v < 0 for k, v in value.items()):
            raise ValueError("Invalid currency threshold")
        return value


class AccountsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    refresh_seconds: int = Field(default=300, ge=60, le=86400)
    timeout_seconds: int = Field(default=30, ge=1, le=60)
    accounts: list[AccountConfig] = Field(default_factory=list, max_length=20)

    @field_validator("accounts")
    @classmethod
    def unique_ids(cls, value):
        if len({a.id for a in value}) != len(value):
            raise ValueError("Account ids must be unique")
        return value


class AccountQueryError(Exception):
    def __init__(self, code="query_failed"):
        self.code = code
        super().__init__(code)


def credential(config, name):
    if not config.credentials_file:
        return secret(name)
    from dotenv import dotenv_values

    path = Path(config.credentials_file)
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            return ""
        return str(dotenv_values(path).get(name) or "").strip()
    except OSError:
        return ""


def amount(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if len(str(value)) > 100:
            return None
        result = Decimal(str(value).replace(",", ""))
        if not result.is_finite() or abs(result) > Decimal("1e18") or result.as_tuple().exponent < -20:
            return None
        return format(result, "f")
    except InvalidOperation:
        return None


def stamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value else None


def money_result(rows, available, config, *, scope):
    if not rows or any(r["amount"] is None or r["currency"] not in {"CNY", "USD", "JPY"} for r in rows):
        raise AccountQueryError("invalid_response")
    status = "ok"
    if available is False:
        status = "exhausted"
    elif all(Decimal(r["amount"]) <= Decimal(str(config.low_balance.get(r["currency"], -1))) for r in rows):
        status = "low"
    return {"status": status, "available": available, "balances": rows, "limits": [], "scope": scope}


def deepseek_result(data, config):
    if not isinstance(data.get("is_available"), bool):
        raise AccountQueryError("invalid_response")
    rows = [{"currency": r.get("currency"), "amount": amount(r.get("total_balance")),
        "cash_amount": amount(r.get("topped_up_balance")), "granted_amount": amount(r.get("granted_balance"))}
        for r in data.get("balance_infos", []) if isinstance(r, dict)]
    return money_result(rows, data["is_available"], config, scope="DeepSeek 账户余额（含赠金）")


def bailian_result(data, config):
    if data.get("Success") is not True or not isinstance(data.get("Data"), dict):
        raise AccountQueryError("invalid_response")
    values = data["Data"]
    balance = amount(values.get("AvailableAmount"))
    rows = [{"currency": values.get("Currency"), "amount": balance,
        "cash_amount": amount(values.get("AvailableCashAmount")), "granted_amount": None}]
    # Cloud balance alone cannot prove a particular model is callable (packages/permissions differ).
    return money_result(rows, False if balance is not None and Decimal(balance) <= 0 else None,
        config, scope="阿里云账户可用额度；不含百炼资源包或免费 Token 剩余量")


def codex_result(data, config, now=None):
    now = time.time() if now is None else now
    buckets = data.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = data.get("rateLimits")
        buckets = {"codex": legacy} if isinstance(legacy, dict) else {}
    limits = []
    for key, bucket in list(buckets.items())[:20]:
        if not isinstance(bucket, dict):
            continue
        windows = []
        for slot in ("primary", "secondary"):
            row = bucket.get(slot)
            if not isinstance(row, dict):
                continue
            used, duration, resets = row.get("usedPercent"), row.get("windowDurationMins"), row.get("resetsAt")
            if (isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used)
                or used < 0 or isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0
                or isinstance(resets, bool) or not isinstance(resets, int) or not 0 < resets < 253402300799):
                continue
            windows.append({"name": slot, "used_percent": used, "remaining_percent": max(0, 100 - used),
                "window_minutes": duration, "resets_at": stamp(resets), "expired": resets <= now})
        credit = bucket.get("credits")
        credits = None
        if isinstance(credit, dict):
            credits = {"balance": amount(credit.get("balance")),
                "has_credits": credit.get("hasCredits") if isinstance(credit.get("hasCredits"), bool) else None,
                "unlimited": credit.get("unlimited") if isinstance(credit.get("unlimited"), bool) else None}
        if windows or credits:
            limits.append({"id": str(key)[:80], "name": str(bucket.get("limitName") or key)[:80],
                "windows": windows, "credits": credits})
    if not limits:
        raise AccountQueryError("invalid_response")
    fresh_windows = [w for b in limits for w in b["windows"] if not w["expired"]]
    status = "low" if any(w["remaining_percent"] <= config.low_remaining_percent for w in fresh_windows) else "ok"
    # Individual buckets can be exhausted while another bucket or paid credits remains usable.
    return {"status": status, "available": None, "balances": [], "limits": limits,
        "scope": "ChatGPT 账号共享额度；credits 按厂商原单位展示，不折算现金", "plan": next(
            (b.get("planType") for b in buckets.values() if isinstance(b, dict) and isinstance(b.get("planType"), str)), None)}


async def stop_process(process):
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def bounded_output(stream):
    result = bytearray()
    while chunk := await stream.read(8192):
        result.extend(chunk)
        if len(result) > MAX_RESPONSE:
            raise AccountQueryError("invalid_response")
    return bytes(result)


def process_environment():
    return {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR", "CODEX_HOME",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY") if k in os.environ}


async def query_codex(config):
    env = process_environment()
    if config.codex_home:
        env["CODEX_HOME"] = config.codex_home
    with tempfile.TemporaryDirectory(prefix="radar-account-") as folder:
        process = await asyncio.create_subprocess_exec(*config.codex_command, "app-server", "--listen", "stdio://",
            cwd=folder, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=MAX_RESPONSE)

        async def send(value):
            process.stdin.write(json.dumps(value).encode() + b"\n")
            await process.stdin.drain()

        async def receive(request_id):
            size = 0
            for _ in range(100):
                line = await process.stdout.readline()
                size += len(line)
                if not line or size > MAX_RESPONSE:
                    raise AccountQueryError("invalid_response")
                obj = json.loads(line)
                if obj.get("id") == request_id:
                    if "error" in obj:
                        # Classify the private error locally; do not store or display its text.
                        message = str(obj["error"].get("message", "")).lower()
                        auth = any(x in message for x in ("unauthorized", "not authenticated", "not logged", "401"))
                        raise AccountQueryError("authorization_required" if auth else "query_failed")
                    return obj["result"]
            raise AccountQueryError("invalid_response")

        try:
            await send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "ai_radar_accounts", "version": "0.12.0"}}})
            await receive(1)
            await send({"method": "initialized", "params": {}})
            # No threads, turns, login changes, purchases or reset-credit consumption.
            await send({"id": 2, "method": "account/rateLimits/read"})
            return codex_result(await receive(2), config)
        finally:
            await stop_process(process)


async def query_account(config):
    if config.provider == "codex":
        return await query_codex(config)
    if config.provider == "deepseek":
        key = credential(config, config.api_key_env)
        if not key:
            raise AccountQueryError("authorization_required")
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            async with client.stream("GET", "https://api.deepseek.com/user/balance",
                headers={"Authorization": "Bearer " + key}) as response:
                if response.status_code in (401, 403):
                    raise AccountQueryError("authorization_required")
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise AccountQueryError("invalid_response")
        return deepseek_result(json.loads(raw), config)
    if config.provider == "bailian":
        ak, sk = credential(config, config.access_key_id_env), credential(config, config.access_key_secret_env)
        if not ak or not sk:
            raise AccountQueryError("authorization_required")
        env = {k: v for k, v in process_environment().items() if k != "CODEX_HOME"}
        env.update(ALIBABA_CLOUD_ACCESS_KEY_ID=ak, ALIBABA_CLOUD_ACCESS_KEY_SECRET=sk,
            ALIBABA_CLOUD_SECURITY_TOKEN=credential(config, config.security_token_env))
        process = await asyncio.create_subprocess_exec(config.billing_python or sys.executable,
            str(Path(__file__).with_name("account_billing.py")), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            data = json.loads(await bounded_output(process.stdout))
            await process.wait()
            if process.returncode or data.get("error"):
                raise AccountQueryError(data.get("error") if data.get("error") in ERRORS else "query_failed")
            return bailian_result(data, config)
        finally:
            await stop_process(process)
    raise AccountQueryError("unsupported")


class AccountMonitor:
    def __init__(self, config_path="", database_path=""):
        self.enabled = bool(config_path and database_path)
        self.config = AccountsConfig.model_validate(tomllib.loads(Path(config_path).read_text())) if self.enabled else AccountsConfig()
        self.path = Path(database_path) if database_path else None
        self.task = None
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.connect() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, checked REAL, success REAL,
                    next_check REAL NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0, owner TEXT,
                    failures INTEGER NOT NULL DEFAULT 0, error TEXT, payload TEXT)""")
            self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=3)

    def fingerprint(self, account):
        raw = account.model_dump_json() + str(self.config.refresh_seconds)
        if account.provider == "deepseek":
            raw += credential(account, account.api_key_env)
        if account.provider == "bailian":
            raw += credential(account, account.access_key_id_env) + credential(account, account.access_key_secret_env) + credential(account, account.security_token_env)
        return hashlib.sha256(raw.encode()).hexdigest()

    def claim(self, account, force=False):
        now, owner, fingerprint = time.time(), uuid.uuid4().hex, self.fingerprint(account)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO accounts(id,fingerprint) VALUES (?,?)", (account.id, fingerprint))
            row = db.execute("SELECT fingerprint,checked,next_check,lease FROM accounts WHERE id=?", (account.id,)).fetchone()
            if row[3] > now:
                return None
            if row[0] != fingerprint:
                db.execute("DELETE FROM accounts WHERE id=?", (account.id,))
                db.execute("INSERT INTO accounts(id,fingerprint) VALUES (?,?)", (account.id, fingerprint))
            elif (row[1] and now - row[1] < 60) or (not force and row[2] > now):
                return None
            db.execute("UPDATE accounts SET lease=?,owner=? WHERE id=?", (now + self.config.timeout_seconds + 15, owner, account.id))
        return owner

    def save(self, account, owner, result=None, error=None):
        now = time.time()
        with self.connect() as db:
            row = db.execute("SELECT failures FROM accounts WHERE id=? AND owner=?", (account.id, owner)).fetchone()
            if not row:
                return
            failures = row[0] + 1 if error else 0
            delay = min(self.config.refresh_seconds, 60 * 2 ** min(failures - 1, 6)) if error else self.config.refresh_seconds
            if error:
                db.execute("UPDATE accounts SET checked=?,next_check=?,lease=0,owner=NULL,failures=?,error=? WHERE id=? AND owner=?",
                    (now, now + delay, failures, error, account.id, owner))
            else:
                db.execute("UPDATE accounts SET checked=?,success=?,next_check=?,lease=0,owner=NULL,failures=0,error=NULL,payload=? WHERE id=? AND owner=?",
                    (now, now, now + delay, json.dumps(result, ensure_ascii=False), account.id, owner))

    async def refresh(self, force=False):
        if not self.enabled:
            return
        # Sequential, bounded queries avoid competing with inference for server resources.
        for account in self.config.accounts:
            try:
                owner = self.claim(account, force)
                if not owner:
                    continue
                try:
                    async with asyncio.timeout(self.config.timeout_seconds):
                        result = await query_account(account)
                    self.save(account, owner, result=result)
                except AccountQueryError as exc:
                    self.save(account, owner, error=exc.code)
                except Exception:
                    self.save(account, owner, error="query_failed")
            except (OSError, sqlite3.Error):
                continue

    def kick(self, force=False):
        if self.enabled and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.refresh(force))

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def report(self):
        now, items = time.time(), []
        if not self.enabled:
            return {"enabled": False, "items": [], "refreshing": False}
        for account in self.config.accounts:
            try:
                with self.connect() as db:
                    row = db.execute("SELECT checked,success,next_check,lease,error,payload FROM accounts WHERE id=? AND fingerprint=?",
                        (account.id, self.fingerprint(account))).fetchone()
            except (OSError, sqlite3.Error):
                row = None
            checked, success, next_check, lease, error, payload = row or (None, None, 0, 0, "not_checked", None)
            try:
                data = json.loads(payload) if payload else {"balances": [], "limits": [], "available": None}
                if not isinstance(data, dict):
                    raise ValueError("Invalid snapshot")
            except ValueError:
                data, error = {"balances": [], "limits": [], "available": None}, "query_failed"
            stale = not success or now - success > self.config.refresh_seconds * 2 or bool(error)
            status = error or ("stale" if stale else data.get("status", "not_checked"))
            items.append({**data, "id": account.id, "provider": account.provider, "name": account.name,
                "in_use": account.in_use, "status": status, "available": None if stale else data.get("available"),
                "stale": stale, "checked_at": stamp(checked), "last_success_at": stamp(success),
                "next_check_at": stamp(next_check), "checking": lease > now,
                "message": ERRORS.get(status, ""), "low_remaining_percent": account.low_remaining_percent})
        return {"enabled": True, "items": items, "as_of": stamp(now),
            "refresh_seconds": self.config.refresh_seconds, "refreshing": bool(self.task and not self.task.done())}

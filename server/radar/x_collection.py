"""Budgeted X search with fair fresh heads and independently resumable older pages.

The page ingestion callback and its checkpoint commit together. Search uses fixed
start/end times, never a moving since_id while a window is being paginated.
"""

import asyncio
import hashlib
import math
import re
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import text

from .config import RadarConfig, secret
from .models import XCollectionState
from .sources import X_EXPANSIONS, X_TWEET_FIELDS, X_USER_FIELDS, SourceUnavailable, get_json, x_page_items

ENDPOINT = "https://api.x.com/2/tweets/search/recent"
CONTROL = "control"
DISCOVERY = "discovery"
MAX_WINDOWS = 8


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalized_handles(handles: list[str]) -> list[str]:
    return sorted({handle.removeprefix("@").lower() for handle in handles
                   if re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", handle)})


def request_budgets(config: RadarConfig, handle_count: int) -> tuple[int, int]:
    total = config.x_request_budget
    if total is None:
        total = config.x_max_pages * (1 + math.ceil(handle_count / 12))
    discovery = config.x_discovery_requests
    if discovery is None:
        discovery = config.x_max_pages
    return total, min(discovery, total - bool(handle_count))


@dataclass
class XCollectionResult:
    read_count: int = 0
    accepted_count: int = 0
    request_count: int = 0
    committed_pages: int = 0
    status: str = "partial"
    message: str = ""
    coverage: dict = field(default_factory=dict)


def record_gap(data: dict, window: dict, reason: str):
    data["gap_count"] = data.get("gap_count", 0) + 1
    gap = {"start": window["start"], "end": window["end"], "reason": reason}
    data["gaps"] = (data.get("gaps", []) + [gap])[-8:]


def complete_window(data: dict, window: dict):
    if window.get("partial_response"):
        record_gap(data, window, "partial_response")
        return
    ranges = sorted(data.get("completed_ranges", []) + [[window["start"], window["end"]]])
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    through = data.get("completed_through") or data["coverage_start"]
    for start, end in merged:
        if start <= through < end:
            through = end
            data["completed_through"] = through
    # With eight live gaps, current disjoint completed ranges are bounded too.
    # Old gaps remain explicit; dropping old ranges cannot advance the watermark.
    data["completed_ranges"] = [pair for pair in merged if pair[1] > through][-16:]


class XCollector:
    def __init__(self, sessions, config: RadarConfig, *, clock=None, force_fresh=False):
        self.sessions, self.config = sessions, config
        self.clock = clock or (lambda: datetime.now(UTC))
        self.force_fresh = force_fresh

    def head_interval(self):
        return timedelta(minutes=self.config.x_head_refresh_minutes) if self.config.x_head_refresh_minutes \
            else timedelta(hours=self.config.x_head_refresh_hours)

    @contextmanager
    def transaction(self):
        with self.sessions() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def owned(self, session, owner):
        row = session.get(XCollectionState, CONTROL)
        if not row or row.data.get("owner") != owner:
            raise SourceUnavailable("partial", "X 采集进度已由另一任务接管，本轮停止。")
        row.data = {**row.data, "lease_until": stamp(self.clock() + timedelta(minutes=5))}
        row.updated_at = stamp(self.clock())

    def initialize(self, handles, owner):
        now = self.clock()
        with self.transaction() as session:
            control = session.get(XCollectionState, CONTROL)
            if control and control.data.get("lease_until", "") > stamp(now):
                raise SourceUnavailable("partial", "X 采集任务仍在运行，本轮未重复请求。")
            if not control:
                control = XCollectionState(id=CONTROL, data={})
                session.add(control)
            control.data = {"owner": owner, "lease_until": stamp(now + timedelta(minutes=5))}
            queries = {"watch:" + handle: f"from:{handle} -is:retweet" for handle in handles}
            queries[DISCOVERY] = self.config.x_query
            for key, query in queries.items():
                row = session.get(XCollectionState, key)
                fingerprint = hashlib.sha256(query.encode()).hexdigest()
                if not row:
                    row = XCollectionState(id=key, data={})
                    session.add(row)
                if row.data.get("query_hash") != fingerprint:
                    data = deepcopy(row.data)
                    for window in data.get("windows", []):
                        record_gap(data, window, "query_changed")
                    row.data = {"query": query, "query_hash": fingerprint, "windows": [],
                                "gap_count": data.get("gap_count", 0), "gaps": data.get("gaps", [])}
                row.updated_at = stamp(now)

    def cleanup(self, data, now):
        cutoff = stamp(now - timedelta(hours=min(self.config.lookback_hours, 167)))
        kept = []
        for window in data.get("windows", []):
            if window["start"] < cutoff:
                record_gap(data, {"start": window["start"], "end": min(window["end"], cutoff)},
                           "outside_recent_window")
                if window["end"] > cutoff:
                    # The expired prefix is irretrievable, but the remaining
                    # suffix must survive. Its old cursor belongs to a different
                    # interval, so replay only this still-searchable fixed range.
                    kept.append({**window, "id": str(uuid4()), "start": cutoff, "next_token": ""})
            else:
                kept.append(window)
        data["windows"] = kept
        return cutoff

    def choose_priority(self, keys, used):
        now = self.clock()
        with self.sessions() as session:
            rows = [session.get(XCollectionState, key) for key in keys
                    if used[key] < self.config.x_max_pages]
            due = [row for row in rows if (self.force_fresh and used[row.id] == 0) or not row.data.get("head_end") or
                   instant(row.data["head_end"]) <= now - self.head_interval()]
            if due:
                row = min(due, key=lambda row: (row.data.get("last_head_attempt_at", ""), row.id))
                return row.id, True
            backlog = [row for row in rows if row.data.get("windows")]
            if backlog:
                row = min(backlog, key=lambda row: (row.data.get("last_backfill_at", ""), row.id))
                return row.id, False
        return None

    def prepare(self, key, head, owner):
        now = self.clock()
        with self.transaction() as session:
            self.owned(session, owner)
            row = session.get(XCollectionState, key)
            data = deepcopy(row.data)
            cutoff = self.cleanup(data, now)
            windows = data["windows"]
            if head:
                uncommitted = [window for window in windows if not window.get("head_committed")]
                if uncommitted:
                    window = uncommitted[-1]
                else:
                    end = stamp(now - timedelta(seconds=30))
                    if data.get("head_end", "") >= end:
                        row.data = data
                        return None
                    start = data.get("head_end") or stamp(
                        instant(end) - timedelta(hours=min(self.config.x_initial_lookback_hours,
                                                          self.config.lookback_hours, 167)))
                    if start < cutoff:
                        if data.get("head_end"):
                            record_gap(data, {"start": start, "end": cutoff}, "outside_recent_window")
                        start = cutoff
                    data.setdefault("coverage_start", start)
                    window = {"id": str(uuid4()), "start": start, "end": end,
                              "next_token": "", "page_size": self.config.x_page_size,
                              "last_page_at": "", "head_committed": False}
                    windows.append(window)
                    while len(windows) > MAX_WINDOWS:
                        record_gap(data, windows.pop(0), "pending_window_limit")
                data["last_head_attempt_at"] = stamp(now)
            else:
                if key == DISCOVERY:
                    windows = [window for window in windows if window["end"] == data.get("head_end")]
                if not windows:
                    row.data = data
                    return None
                window = min(windows, key=lambda item: (item["last_page_at"], item["start"]))
                data["last_backfill_at"] = stamp(now)
            data["last_attempt_at"] = stamp(now)
            row.data, row.updated_at = data, stamp(now)
            params = {"query": data["query"], "max_results": window["page_size"],
                      "start_time": window["start"], "end_time": window["end"], "sort_order": "recency",
                      "tweet.fields": X_TWEET_FIELDS,
                      "expansions": X_EXPANSIONS,
                      "user.fields": X_USER_FIELDS}
            if window["next_token"]:
                params["next_token"] = window["next_token"]
            return window["id"], params

    def save_page(self, key, window_id, body, items, owner, ingest_page):
        with self.transaction() as session:
            self.owned(session, owner)
            row = session.get(XCollectionState, key)
            data = deepcopy(row.data)
            window = next(item for item in data["windows"] if item["id"] == window_id)
            accepted = ingest_page(session, items)
            meta = body.get("meta", {})
            if not window["head_committed"]:
                window["head_committed"] = True
                data["head_end"], data["last_head_at"] = window["end"], stamp(self.clock())
                if meta.get("newest_id"):
                    data["newest_id"] = str(meta["newest_id"])
            window["last_page_at"] = stamp(self.clock())
            window["partial_response"] = window.get("partial_response", False) or bool(body.get("errors"))
            window["next_token"] = meta.get("next_token") or ""
            if not window["next_token"]:
                complete_window(data, window)
                data["windows"].remove(window)
            data["last_success_at"] = stamp(self.clock())
            row.data, row.updated_at = data, stamp(self.clock())
            return accepted

    def reset_cursor(self, key, window_id, owner):
        with self.transaction() as session:
            self.owned(session, owner)
            row = session.get(XCollectionState, key)
            data = deepcopy(row.data)
            for window in data["windows"]:
                if window["id"] == window_id:
                    window["next_token"] = ""
                    window["cursor_restarts"] = window.get("cursor_restarts", 0) + 1
            row.data = data

    def coverage(self, handles):
        now = self.clock()
        keys = ["watch:" + handle for handle in handles]
        discovery_enabled = request_budgets(self.config, len(handles))[1] > 0
        enabled_keys = keys + ([DISCOVERY] if discovery_enabled else [])
        with self.sessions() as session:
            rows = [session.get(XCollectionState, key) for key in enabled_keys]
            summaries = []
            for key, row in zip(enabled_keys, rows, strict=True):
                data = row.data if row else {}
                summaries.append({"scope": key, "head_end": data.get("head_end", ""),
                                  "completed_through": data.get("completed_through", ""),
                                  "pending_windows": len(data.get("windows", [])),
                                  "gap_count": data.get("gap_count", 0),
                                  "partial_response": any(window.get("partial_response") for window in data.get("windows", [])) or
                                                      any(gap["reason"] == "partial_response" for gap in data.get("gaps", [])),
                                  "fresh": bool(data.get("head_end") and
                                                instant(data["head_end"]) > now - self.head_interval())})
        return {"watched_total": len(keys),
                "watched_fresh": sum(item["fresh"] for item in summaries if item["scope"] != DISCOVERY),
                "enabled_scopes": len(enabled_keys), "discovery_enabled": discovery_enabled,
                "discovery_fresh": any(item["fresh"] for item in summaries if item["scope"] == DISCOVERY),
                "partial_response": any(item["partial_response"] for item in summaries),
                "pending_windows": sum(item["pending_windows"] for item in summaries),
                "gap_count": sum(item["gap_count"] for item in summaries), "scopes": summaries}

    async def collect(self, client, handles, ingest_page) -> XCollectionResult:
        token = secret("X_BEARER_TOKEN")
        if not token:
            raise SourceUnavailable("auth_required", "需要 X Developer Bearer Token；普通登录不等于 API 授权。")
        handles = normalized_handles(handles)
        total_budget, discovery_budget = request_budgets(self.config, len(handles))
        owner, result, used = str(uuid4()), XCollectionResult(), Counter()
        self.initialize(handles, owner)
        error_message = ""
        try:
            async def fetch(key, head):
                prepared = self.prepare(key, head, owner)
                if prepared is None:
                    used[key] = self.config.x_max_pages
                    return False
                window_id, params = prepared
                used[key] += 1
                result.request_count += 1
                try:
                    async with asyncio.timeout(30):
                        body = await get_json(client, ENDPOINT, params=params,
                                              headers={"Authorization": f"Bearer {token}"}, allow_partial=True)
                    # A completed watermark needs explicit pagination metadata;
                    # the shared item parser also serves callers without cursors.
                    if not isinstance(body.get("meta"), dict):
                        raise ValueError("Invalid X pagination metadata")
                    if not isinstance(body["meta"].get("next_token", ""), str):
                        raise ValueError("Invalid X pagination token")
                    items = x_page_items(body)
                    accepted = self.save_page(key, window_id, body, items, owner, ingest_page)
                    result.read_count += len(items)
                    result.accepted_count += accepted
                    result.committed_pages += 1
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 400 and params.get("next_token"):
                        self.reset_cursor(key, window_id, owner)
                        raise SourceUnavailable("error", "X 分页令牌被拒绝；已保存进度，下轮在原时间窗口重试。") from exc
                    raise
                return True

            priority_budget = total_budget - discovery_budget
            while result.request_count < priority_budget:
                choice = self.choose_priority(["watch:" + handle for handle in handles], used)
                if not choice:
                    break
                await fetch(*choice)
            # Reallocate (never increase) the reserved discovery requests when
            # watched accounts are overdue. Once their heads are fresh, broad
            # discovery keeps its normal allocation; older pages stay bounded.
            if self.config.x_watch_freshness_first:
                while result.request_count < total_budget:
                    choice = self.choose_priority(["watch:" + handle for handle in handles], used)
                    if not choice or not choice[1]:
                        break
                    await fetch(*choice)
            # Discovery never follows an old window before its current fresh head.
            for page in range(discovery_budget):
                if result.request_count >= total_budget:
                    break
                if not await fetch(DISCOVERY, page == 0):
                    break
        except SourceUnavailable as exc:
            result.status, error_message = exc.status, exc.message
        except Exception as exc:
            result.status, error_message = "error", f"X 请求或保存中断（{type(exc).__name__}），下轮自动恢复。"
        finally:
            with self.transaction() as session:
                row = session.get(XCollectionState, CONTROL)
                if row and row.data.get("owner") == owner:
                    row.data = {"owner": "", "lease_until": ""}
                    row.updated_at = stamp(self.clock())
        result.coverage = self.coverage(handles)
        incomplete = (not result.coverage["enabled_scopes"] or
                      result.coverage["watched_fresh"] < len(handles) or
                      (result.coverage["discovery_enabled"] and not result.coverage["discovery_fresh"]) or
                      result.coverage["pending_windows"] or result.coverage["gap_count"])
        if not error_message:
            result.status = "partial" if incomplete else "healthy"
        result.coverage.update(request_budget=total_budget, requests_used=result.request_count,
                               budget_exhausted=result.request_count >= total_budget and bool(incomplete))
        result.message = (
            f"本轮读取 {result.read_count} 条，新增有效信息 {result.accepted_count} 条；"
            f"已用 {result.request_count}/{total_budget} 次请求。"
            f"最近 {int(self.head_interval().total_seconds() / 60)} 分钟已拉取 {result.coverage['watched_fresh']}/{len(handles)} 个关注账号最新窗口；"
            f"{result.coverage['pending_windows']} 个窗口仍有待补页，{result.coverage['gap_count']} 个历史覆盖缺口。"
        )
        if result.coverage["budget_exhausted"]:
            result.message += "本轮请求预算已用完，下轮自动轮转续采。"
        if result.committed_pages:
            result.message += "已保留已取得且符合筛选条件的内容。"
        if not result.coverage["enabled_scopes"]:
            result.message += "未启用关注账号或广泛发现，本轮未执行 X 请求，尚无已覆盖来源。"
        elif not discovery_budget:
            result.message += "本轮广泛发现未启用，覆盖统计仅包括关注账号。"
        if result.coverage["partial_response"]:
            result.message += "平台部分返回信息不完整，可能仅涉及引用或附加信息；可读主帖已保留。"
        if self.config.x_watch_freshness_first and discovery_budget and used[DISCOVERY] == 0:
            result.message += "本轮优先补齐关注账号的最新窗口，广泛发现等待后续额度；总请求上限未增加。"
        result.message += error_message
        return result

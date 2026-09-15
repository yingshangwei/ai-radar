"""Free-source collection, immutable evidence and bounded industry research."""

import asyncio
import hashlib
import json
import logging
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select

from . import usage
from .industry_config import THEMES
from .industry_contracts import POLICY, IndustryAudit, prompt, validate_report
from .industry_models import IndustryAssessment, IndustryEvidence, IndustryTracking
from .industry_sources import ENTITIES, SOURCES, EvidenceInput, fetch_source
from .models import Article, SourceState
from .providers import make_provider
from .ranking import canonicalize

logger = logging.getLogger(__name__)
LIMITATIONS = [
    "覆盖部分 A 股、阿里巴巴港股公告和海外主体；公司样本及政策来源不代表全市场。",
    "行业状态是有条件的经营证据判断，不是股票涨跌概率；观察名单不代表买入推荐。",
    "未接入即时股价、估值和市场一致预期，预期差未知；不能据此确定买卖价格。",
    "免费来源包含公告摘要、申报通知和部分正文；发布时间不等于事件发生时间。",
]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def utc(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("missing_timezone")
    return result.astimezone(UTC)


def matched_themes(title, text, entities, kind):
    result = {theme for uid in entities for theme in ENTITIES.get(uid, {}).get("themes", []) if theme in THEMES}
    body = title + "\n" + text[:60000]
    for key, theme in THEMES.items():
        if any(re.search(term, body, re.I) for term in theme["terms"]):
            result.add(key)
    if kind in ("macro", "macro_policy", "policy", "policy_notice"):
        result.update(THEMES)
    return sorted(result)


def public_evidence(row):
    fields = ("id", "source_id", "source_name", "url", "title", "text", "published_at",
              "published_precision", "first_seen_at", "kind", "entity_ids", "theme_ids", "partial", "revision")
    return {key: getattr(row, key) for key in fields}


def put_evidence(session, item, source_name, *, clock=None):
    """One current version per canonical original; previous evidence is never rewritten."""
    now = clock or datetime.now(UTC)
    published = utc(item.published_at)
    if published > now or not item.title.strip() or not item.text.strip():
        return 0
    url = urlsplit(item.url)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        return 0
    entities = sorted({uid for uid in item.entity_ids if uid in ENTITIES})
    theme_ids = matched_themes(item.title, item.text, entities, item.kind)
    if not theme_ids:
        return 0
    canonical = canonicalize(item.url)
    document_key = digest(canonical)
    content = {"title": item.title[:2000], "text": item.text[:60000], "published_at": published.isoformat(),
               "published_precision": item.published_precision, "kind": item.kind,
               "partial": bool(item.metadata.get("partial", True)), "entity_ids": entities}
    fingerprint = digest(content)
    uid = digest([document_key, fingerprint])
    if session.get(IndustryEvidence, uid):
        return 0  # An old source version cannot roll back a newer saved version.
    previous = session.scalar(select(IndustryEvidence).where(
        IndustryEvidence.document_key == document_key, IndustryEvidence.current.is_(True),
    ))
    # A shorter repeat from the existing news cache must not replace richer direct evidence.
    if previous and item.source_id.startswith("radar:") and not previous.source_id.startswith("radar:"):
        return 0
    if previous:
        # A failed optional full-text request is a coverage change, not a source edit.
        scopes = {"filing_metadata": 0, "headline_only": 0, "feed_summary": 1,
                  "source_excerpt": 1, "existing_free_source_excerpt": 1, "policy_abstract": 1,
                  "feed_content": 2, "publisher_article": 3, "filing_primary_document": 3}
        before = scopes.get(previous.details.get("evidence_scope", "source_excerpt"), 1)
        after = scopes.get(item.metadata.get("evidence_scope", "source_excerpt"), 1)
        if (after < before or previous.kind == "filing_document" and item.kind == "filing_notice"
                or not previous.partial and content["partial"]):
            return 0
    if previous:
        previous.current = False
    origin = ("company:" + entities[0] if len(entities) == 1 else
              "agency:fed" if "federalreserve.gov" in url.hostname else
              "agency:federalregister" if "federalregister.gov" in url.hostname else url.hostname.removeprefix("www."))
    session.add(IndustryEvidence(
        id=uid, document_key=document_key, content_hash=fingerprint, source_id=item.source_id,
        source_name=source_name, origin=origin, external_id=item.external_id[:300], url=canonical,
        first_seen_at=now.isoformat(), theme_ids=theme_ids, revision=(previous.revision + 1 if previous else 1),
        details={"evidence_scope": str(item.metadata.get("evidence_scope", "source_excerpt"))[:500],
                 "region": "cn" if item.source_id == "ndrc-policy" or any(
                     ENTITIES[e].get("region") == "cn" for e in entities) else "global"}, **content,
    ))
    session.flush()
    return 1


class IndustryService:
    def __init__(self, sessions, config, *, provider_factory=None, clock=None):
        self.sessions, self.config, self.options = sessions, config, config.industry
        self.provider_factory = provider_factory or make_provider
        self.clock = clock or (lambda: datetime.now(UTC))
        self.collect_lock, self.analysis_lock = asyncio.Lock(), asyncio.Lock()
        self.configuration = digest({"policy": POLICY, "provider": config.provider.model_dump(),
                                     "review_provider": (config.summary_review.provider or config.provider).model_dump()})
        if self.options.enabled:
            with self.transaction() as session:
                for key in THEMES:
                    if not session.get(IndustryTracking, key):
                        session.add(IndustryTracking(id=key))
                for key, spec in SOURCES.items():
                    if not session.get(SourceState, "industry:" + key):
                        session.add(SourceState(id="industry:" + key, name=spec.name, platform="industry"))
            self.recover()

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as session:
            if session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session

    def recover(self):
        # A reserved external call is never repeated after a timeout or process loss.
        with self.transaction() as session:
            for row in session.scalars(select(IndustryAssessment).where(
                IndustryAssessment.status == "calling", IndustryAssessment.lease_until <= self.clock().isoformat(),
            )):
                response = next((event for event in reversed(row.history) if event.get("event") == "response"
                                 and event.get("stage") == row.stage and event.get("owner") == row.owner), None)
                if response is not None:
                    try:
                        parsed = self._parse(response["text"], row.stage, row.evidence)
                        self._apply_result(row, parsed)
                    except (ValueError, TypeError, KeyError):
                        row.status, row.failure_code = "needs_attention", "invalid_response"
                else:
                    row.status, row.failure_code = "outcome_unknown", "interrupted_call"
                row.owner, row.lease_until = "", ""
                row.updated_at = self.clock().isoformat()

    @staticmethod
    def _parse(value, stage, evidence):
        if not isinstance(value, str) or len(value) > 60000:
            raise ValueError("invalid_response")
        return IndustryAudit.model_validate_json(value) if stage == "audit" else validate_report(value, evidence)

    def _apply_result(self, row, parsed):
        if row.stage == "audit":
            row.audit = parsed.model_dump()
            if parsed.passed:
                validate_report(json.dumps(row.candidate), row.evidence)
                row.result, row.status, row.completed_at = row.candidate, "ready", self.clock().isoformat()
            elif row.corrections < 1:
                row.stage, row.status = "correction", "pending"
            else:
                row.status, row.failure_code = "needs_attention", "audit_rejected"
        else:
            row.candidate = parsed.model_dump()
            if row.stage == "correction":
                row.corrections += 1
            row.stage, row.status = "audit", "pending"

    def _enabled_themes(self, session):
        return {row.id for row in session.scalars(select(IndustryTracking).where(IndustryTracking.enabled.is_(True)))}

    def due(self):
        if not self.options.enabled:
            return False
        with self.sessions() as session:
            if not self._enabled_themes(session):
                return False
            for key, source in SOURCES.items():
                state = session.get(SourceState, "industry:" + key)
                if not state or not state.last_attempt_at or self.clock() - utc(state.last_attempt_at) >= timedelta(
                    minutes=max(source.refresh_minutes, self.options.collect_minutes),
                ):
                    return True
        return False

    async def collect(self):
        if not self.options.enabled:
            return {"added": 0, "checked": 0, "failed": 0}
        async with self.collect_lock:
            total, checked, failed = 0, 0, 0
            with self.sessions() as session:
                if not self._enabled_themes(session):
                    return {"added": 0, "checked": 0, "failed": 0}
            async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={
                "User-Agent": "AIRadar/0.1 (personal research; https://radar.yswdra.cn)",
            }) as client:
                for key, source in SOURCES.items():
                    with self.transaction() as session:
                        state = session.get(SourceState, "industry:" + key)
                        if state.last_attempt_at and self.clock() - utc(state.last_attempt_at) < timedelta(
                            minutes=max(source.refresh_minutes, self.options.collect_minutes),
                        ):
                            continue
                        state.last_attempt_at = self.clock().isoformat()
                    checked += 1
                    try:
                        items = await fetch_source(client, source, lookback_days=self.options.lookback_days,
                                                   max_items=self.options.max_items_per_source)
                        with self.transaction() as session:
                            added = sum(put_evidence(session, item, source.name, clock=self.clock()) for item in items)
                            state = session.get(SourceState, "industry:" + key)
                            state.status, state.last_success_at = "healthy", self.clock().isoformat()
                            state.item_count = len(items)
                            state.message = f"本次读取 {len(items)} 条，保存 {added} 个新版本；仅覆盖所列免费来源。"
                            total += added
                    except Exception as exc:
                        failed += 1
                        with self.transaction() as session:
                            state = session.get(SourceState, "industry:" + key)
                            state.status = "error"
                            state.message = {
                                "blocked": "来源拒绝服务器访问（HTTP 401/403）；历史数据保留，按周期复查访问状态。",
                                "rate_limited": "来源请求限流（HTTP 429）；历史数据保留，冷却后按周期重试。",
                            }.get(getattr(exc, "code", ""), "来源暂不可用或响应未通过校验；已保留历史数据，稍后按周期重试。")
                        logger.warning("Industry source %s failed: %s", key, type(exc).__name__)
            total += self.reuse_free_articles()
            return {"added": total, "checked": checked, "failed": failed}

    def reuse_free_articles(self):
        cutoff = (self.clock() - timedelta(days=self.options.lookback_days)).isoformat()
        with self.transaction() as session:
            rows = session.scalars(select(Article).where(
                Article.platform.in_(("rss", "web")), Article.published_at >= cutoff,
            ).order_by(Article.published_at.desc()).limit(200)).all()
            total = 0
            for row in rows:
                # Reuse only the registered official/RSS/research free sources; never acquire more X data.
                allowed = {feed.id for feed in self.config.feeds} | {
                    "anthropic", "hf-papers", "arxiv-theory", *["official-" + key for key in self.config.official_news_sources],
                }
                if row.source_id not in allowed:
                    continue
                item = EvidenceInput(source_id="radar:" + row.source_id, external_id=row.id, url=row.url,
                    title=row.title, text=row.text, published_at=row.published_at,
                    published_precision=row.published_precision, kind="technology", entity_ids=[],
                    metadata={"partial": True, "evidence_scope": "existing_free_source_excerpt"})
                total += put_evidence(session, item, row.author or row.source_id, clock=self.clock())
            return total

    def _query(self, theme=None):
        query = select(IndustryEvidence).where(
            IndustryEvidence.current.is_(True),
            IndustryEvidence.published_at >= (self.clock() - timedelta(days=self.options.lookback_days)).isoformat(),
            IndustryEvidence.published_at <= self.clock().isoformat(),
            IndustryEvidence.first_seen_at <= self.clock().isoformat(),
        )
        if theme:
            query = query.where(IndustryEvidence.theme_ids.contains(theme))
        return query.order_by(IndustryEvidence.published_at.desc(), IndustryEvidence.id)

    def evidence(self, theme=None, q="", limit=30, offset=0):
        with self.sessions() as session:
            query = self._query(theme)
            if q:
                query = query.where(IndustryEvidence.title.contains(q, autoescape=True)
                                    | IndustryEvidence.text.contains(q, autoescape=True))
            total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
            return {"items": [public_evidence(row) for row in session.scalars(query.limit(limit).offset(offset))],
                    "total": total}

    def _select_evidence(self, session, theme):
        rows = session.scalars(self._query(theme).limit(250)).all()
        # Prefer actual text, then diversify origins. Metadata is still visible in the evidence list.
        rows.sort(key=lambda row: row.kind in ("filing_notice", "macro", "macro_policy", "policy", "policy_notice"))
        # Reserve comparable visibility for domestic evidence when available.
        domestic = list(session.scalars(self._query(theme).where(
            IndustryEvidence.details["region"].as_string() == "cn").limit(125)))
        domestic.sort(key=lambda row: row.kind in ("filing_notice", "policy", "policy_notice"))
        global_rows = [r for r in rows if r.details.get("region") != "cn"]
        rows = [r for pair in zip_longest(domestic, global_rows) for r in pair if r]
        chosen, counts, chars = [], {}, 0
        for cap in (1, 3):
            for row in rows:
                if any(item["id"] == row.id for item in chosen) or counts.get(row.origin, 0) >= cap:
                    continue
                room = self.options.max_evidence_chars - chars
                if len(chosen) >= self.options.max_evidence or room < 400:
                    break
                excerpt = row.text[:min(5000, room)]
                chosen.append({**public_evidence(row), "text": excerpt, "origin": row.origin,
                               "partial": row.partial or len(excerpt) < len(row.text)})
                chars += len(excerpt)
                counts[row.origin] = counts.get(row.origin, 0) + 1
        return chosen

    def _day_start(self):
        return self.clock().astimezone(ZoneInfo(self.config.timezone)).replace(
            hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).isoformat()

    def _budget(self, session):
        return sum(1 for row in session.scalars(select(IndustryAssessment)) for event in row.history
                   if event.get("event") == "call_reserved" and event.get("at", "") >= self._day_start())

    def _next(self, session):
        if not self.options.enabled or not self.options.analysis_enabled or self.config.provider.kind == "extractive":
            return None
        if self.collect_lock.locked():
            return None  # Freeze a report after the current source sweep has finished.
        if self._budget(session) >= self.options.max_calls_per_day:
            return None
        enabled = self._enabled_themes(session)
        pending = session.scalar(select(IndustryAssessment).where(
            IndustryAssessment.status == "pending", IndustryAssessment.theme_id.in_(enabled),
        ).order_by(IndustryAssessment.created_at).limit(1))
        if pending:
            return pending
        for theme_id in THEMES:
            if theme_id not in enabled:
                continue
            recent = session.scalar(select(IndustryAssessment).where(
                IndustryAssessment.theme_id == theme_id,
            ).order_by(IndustryAssessment.created_at.desc()).limit(1))
            if recent and self.clock() - utc(recent.created_at) < timedelta(hours=self.options.analysis_hours):
                continue
            count = session.scalar(select(func.count()).select_from(IndustryAssessment).where(
                IndustryAssessment.theme_id == theme_id, IndustryAssessment.created_at >= self._day_start(),
            ))
            if count >= self.options.max_snapshots_per_theme_per_day:
                continue
            evidence = self._select_evidence(session, theme_id)
            if not evidence:
                continue
            fingerprint = digest([[item["id"], item["text"]] for item in evidence])
            key = digest([theme_id, fingerprint, self.configuration])
            if session.get(IndustryAssessment, key):
                continue
            return {"id": key, "theme_id": theme_id, "fingerprint": fingerprint, "evidence": evidence,
                    "context": {key: value for key, value in THEMES[theme_id].items() if key != "terms"}}
        return None

    def has_pending(self):
        if not self.options.enabled:
            return False
        self.recover()
        with self.sessions() as session:
            return self._next(session) is not None

    async def analyze(self):
        """One externally reserved stage per queue slice; known results survive restart."""
        async with self.analysis_lock:
            self.recover()
            with self.transaction() as session:
                item = self._next(session)
                if item is None:
                    return {"processed": 0, "status": "idle"}
                if isinstance(item, dict):
                    item = IndustryAssessment(**item, as_of=self.clock().isoformat(),
                                              created_at=self.clock().isoformat())
                    session.add(item)
                    session.flush()
                uid, stage, owner = item.id, item.stage, uuid4().hex
                provider_config = (self.config.summary_review.provider or self.config.provider
                                   if stage == "audit" else self.config.provider)
                timeout = min(900, provider_config.timeout_seconds * 3 + 30)
                item.status, item.owner = "calling", owner
                item.lease_until = (self.clock() + timedelta(seconds=timeout + 60)).isoformat()
                item.history = [*item.history, {"event": "call_reserved", "stage": stage,
                                               "at": self.clock().isoformat(), "owner": owner}]
                evidence, context, as_of = item.evidence, item.context, item.as_of
                candidate = item.candidate if stage != "generation" else None
                audit = item.audit if stage == "correction" else None
                text, schema = prompt(context, evidence, as_of, candidate=candidate, audit=audit)
            try:
                with usage.scope("industry_research", stage):
                    async with asyncio.timeout(timeout):
                        value = await self.provider_factory(provider_config).complete(text, schema)
                # Persist the returned bytes before parsing: malformed output is known, not an unknown call.
                with self.transaction() as session:
                    row = session.get(IndustryAssessment, uid)
                    if row.owner != owner or row.status != "calling":
                        return {"processed": 1, "status": "outcome_unknown"}
                    row.history = [*row.history, {"event": "response", "stage": stage,
                                                 "at": self.clock().isoformat(), "owner": owner,
                                                 "text": value if len(value) <= 60000 else ""}]
                parsed = self._parse(value, stage, evidence)
            except BaseException as exc:
                known = False
                with self.transaction() as session:
                    row = session.get(IndustryAssessment, uid)
                    if row.owner == owner:
                        known = any(event.get("event") == "response" and event.get("stage") == stage
                                    and event.get("owner") == owner
                                    for event in row.history)
                        row.status = "needs_attention" if known else "outcome_unknown"
                        row.failure_code = "invalid_response" if known else "unconfirmed_provider_result"
                        row.owner, row.lease_until, row.updated_at = "", "", self.clock().isoformat()
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                logger.warning("Industry analysis %s stopped: %s", uid[:10], type(exc).__name__)
                return {"processed": 1, "status": "needs_attention" if known else "outcome_unknown"}
            with self.transaction() as session:
                row = session.get(IndustryAssessment, uid)
                if row.owner != owner or row.status != "calling":
                    return {"processed": 1, "status": "outcome_unknown"}
                row.owner, row.lease_until, row.updated_at = "", "", self.clock().isoformat()
                self._apply_result(row, parsed)
                return {"processed": 1, "status": row.status}

    @staticmethod
    def _report(row):
        if row is None or row.status != "ready":
            return None
        return {"id": row.id, "status": "ready", "as_of": row.as_of, "created_at": row.created_at,
                "evidence_ids": [item["id"] for item in row.evidence], **row.result}

    def overview(self):
        with self.sessions() as session:
            sources = []
            for key, spec in SOURCES.items():
                state = session.get(SourceState, "industry:" + key)
                sources.append({"id": key, "name": spec.name, "description": spec.description,
                    "url": spec.url, "refresh_minutes": max(spec.refresh_minutes, self.options.collect_minutes),
                    "status": state.status if state and self.options.enabled else "disabled",
                    "message": state.message if state else "尚未启用采集",
                    "last_attempt_at": state.last_attempt_at if state else None,
                    "last_success_at": state.last_success_at if state else None,
                    "item_count": state.item_count if state else 0,
                    "limitations": spec.limitations})
            themes = []
            for key, theme in THEMES.items():
                evidence = session.scalars(self._query(key)).all()
                reports = session.scalars(select(IndustryAssessment).where(
                    IndustryAssessment.theme_id == key, IndustryAssessment.status == "ready",
                ).order_by(IndustryAssessment.created_at.desc()).limit(2)).all()
                latest = session.scalar(select(IndustryAssessment).where(
                    IndustryAssessment.theme_id == key,
                ).order_by(IndustryAssessment.created_at.desc()).limit(1))
                tracking = session.get(IndustryTracking, key)
                themes.append({**{k: v for k, v in theme.items() if k != "terms"},
                    "enabled": tracking.enabled if tracking else True,
                    "companies": [{k: entity[k] for k in ("id", "name", "ticker", "exchange")}
                                  for entity in sorted(ENTITIES.values(), key=lambda e: e.get("region") != "cn")
                                  if key in entity.get("themes", [])],
                    "evidence_count": len(evidence), "independent_sources": len({row.origin for row in evidence}),
                    "latest_evidence_at": evidence[0].published_at if evidence else None,
                    "state": reports[0].result["state"] if reports else "insufficient_evidence",
                    "assessment": self._report(reports[0]) if reports else None,
                    "previous_assessment": self._report(reports[1]) if len(reports) > 1 else None,
                    "analysis_status": {"status": latest.status, "failure_code": latest.failure_code,
                                        "updated_at": latest.updated_at} if latest else None})
            return {"enabled": self.options.enabled, "free_only": True, "as_of": self.clock().isoformat(),
                    "scope": "AI 跨行业经营研究 · 国内与海外 · 免费原始来源",
                    "limitations": LIMITATIONS, "sources": sources, "themes": themes,
                    "budget": {"calls_today": self._budget(session), "max_calls_per_day": self.options.max_calls_per_day,
                               "unit": "model_stage", "provider_calls_per_stage_max": 2,
                               "analysis_enabled": self.options.analysis_enabled}}

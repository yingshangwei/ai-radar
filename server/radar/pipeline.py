import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from .article_presentation import ArticlePresentationService
from .config import RadarConfig
from .digest_selection import mark_supplemental_stories, select_digest_articles
from .models import Article, ArticleReading, Digest, Job, SourceState, Watch, now_iso
from .providers import make_provider
from .ranking import article_id, canonicalize, classify, engagement, rank
from .reading import ReadingService, cache_research_abstract, remember_references, sync_documents
from .research import ResearchResult, fetch_arxiv_theory, fetch_hf_papers
from .schemas import IncomingArticle
from .sources import SourceUnavailable, fetch_anthropic, fetch_facebook, fetch_rss
from .summary_evidence import digest_review_evidence, reserve_publication, review_evidence_fingerprint
from .summary_review import SummaryReviewPending, SummaryReviewService
from .translation import TranslationService, queue_article
from .x_collection import XCollectionResult, XCollector

logger = logging.getLogger(__name__)
RESEARCH_SOURCES = {"hf-papers", "arxiv-theory"}


def as_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def merge_research_item(session, item, existing):
    """Keep one paper across discovery sources without discarding richer evidence."""
    if not existing or item.source_id not in RESEARCH_SOURCES or existing.source_id not in RESEARCH_SOURCES:
        return item
    metrics = {**existing.metrics, **item.metrics}
    old_version = existing.metrics.get("arxiv_version", 0)
    new_version = item.metrics.get("arxiv_version", 0)
    updates = {"metrics": metrics}
    if old_version > new_version:
        # HF metadata may be unversioned. It can update votes, not roll back a
        # versioned source abstract or its exact original publication timestamp.
        updates.update({key: getattr(existing, key) for key in (
            "title", "text", "author", "published_at", "published_precision")})
        updates["published_at"] = datetime.fromisoformat(existing.published_at)
        metrics["arxiv_version"] = old_version
    if existing.source_id == "hf-papers":
        updates["source_id"] = existing.source_id
    reading = session.get(ArticleReading, existing.id)
    references = {ref["url"]: ref for ref in (reading.references if reading else [])}
    references.update({ref.url: ref.model_dump(mode="json") for ref in item.references})
    return IncomingArticle.model_validate({**item.model_dump(), **updates,
                                          "references": list(references.values())[:30]})


def ingest(session, items: list[IncomingArticle], config: RadarConfig, authority: float = 0) -> int:
    handles = {w.handle.lower() for w in session.scalars(select(Watch).where(Watch.enabled.is_(True)))}
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=config.lookback_hours)
    accepted = 0
    for item in items:
        if not cutoff <= item.published_at <= now + timedelta(minutes=5):
            continue
        uid = article_id(item)
        existing = session.get(Article, uid)
        item = merge_research_item(session, item, existing)
        topics = classify(item)
        if not topics:
            continue
        item_authority = max(authority, 1.5) if item.source_id == "hf-papers" else authority
        priority = item.handle.lower() in handles or item_authority >= 1.5
        if (
            item.platform in ("x", "facebook")
            and not priority
            and engagement(item.metrics) < config.min_engagement
        ):
            continue
        values = item.model_dump(exclude={"published_at", "references"})
        values.update(
            id=uid,
            published_at=item.published_at.isoformat(),
            canonical_url=canonicalize(item.url),
            topics=topics,
            priority=priority,
            score=rank(item, priority, item_authority, now),
        )
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
        else:
            existing = Article(**values)
            session.add(existing)
            accepted += 1
        session.flush()
        queue_article(session, existing, config.translation)
        remember_references(session, existing, [r.model_dump(mode="json") for r in item.references])
        if config.reading.enabled:
            cache_research_abstract(session, existing, config)
            sync_documents(session, existing, config)
    return accepted


def digest_window(day: date, config: RadarConfig):
    # The edition is the 24 hours ending at configured local delivery time, including DST transitions.
    end = datetime.combine(day, time(config.daily_hour, config.daily_minute), ZoneInfo(config.timezone))
    start = end - timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def publication_is_current(session, day: date, force: bool, expected: dict | None) -> bool:
    """Reserve the short write transaction and preserve concurrent publication."""
    reserve_publication(session)
    current = session.get(Digest, day.isoformat())
    if current is not None and not force:
        return False
    if force and (as_dict(current) if current is not None else None) != expected:
        raise SummaryReviewPending("日报已更新，已保留新发布的版本。")
    return True


class Pipeline:
    def __init__(self, sessions, config: RadarConfig):
        self.sessions, self.config = sessions, config
        self.lock = asyncio.Lock()
        self.collect_lock = asyncio.Lock()
        self.translate_lock = asyncio.Lock()
        self.translations = TranslationService(sessions, config.translation)
        self.reading = ReadingService(sessions, config, self.translations)
        self.summary_reviews = SummaryReviewService(sessions, config)
        self.presentations = ArticlePresentationService(sessions, config, lambda value: make_provider(value))
        with sessions.begin() as session:
            sources = [("x", "X / Twitter", "x"), ("facebook", "Facebook", "facebook")]
            sources += [(f.id, f.name, "rss") for f in config.feeds]
            if config.anthropic_news_enabled:
                sources.append(("anthropic", "Anthropic News", "web"))
            if config.research.hf_enabled:
                sources.append(("hf-papers", "Hugging Face 热门论文", "web"))
            if config.research.arxiv_enabled:
                sources.append(("arxiv-theory", "arXiv 前沿理论", "web"))
            for key, name, platform in sources:
                if not session.get(SourceState, key):
                    session.add(SourceState(id=key, name=name, platform=platform))
            for article in session.scalars(select(Article)):
                queue_article(session, article, config.translation)

    def research_due(self, source_id):
        with self.sessions() as session:
            state = session.get(SourceState, source_id)
            if not state or not state.last_attempt_at:
                return True
            hours = self.config.research.refresh_hours if state.status == "healthy" else 1
            return datetime.fromisoformat(state.last_attempt_at) <= datetime.now(UTC) - timedelta(hours=hours)

    async def collect(self):
        with self.sessions() as session:
            handles = list(session.scalars(select(Watch.handle).where(
                Watch.enabled.is_(True), Watch.platform == "x",
            )))
        async with httpx.AsyncClient(
            timeout=25, headers={"User-Agent": "AIRadar/0.1 (+personal intelligence reader)"}
        ) as client:
            entries = [
                ("x", 0, lambda: XCollector(self.sessions, self.config).collect(
                    client, handles, lambda session, items: ingest(session, items, self.config),
                )),
                ("facebook", 0, lambda: fetch_facebook(client, self.config)),
            ]
            entries += [
                (f.id, f.authority, lambda feed=f: fetch_rss(client, feed)) for f in self.config.feeds
            ]
            if self.config.anthropic_news_enabled:
                entries.append(("anthropic", 2.0, lambda: fetch_anthropic(client)))
            if self.config.research.hf_enabled and self.research_due("hf-papers"):
                entries.append(("hf-papers", 1.5, lambda: fetch_hf_papers(
                    client, self.config.research, self.config.lookback_hours)))
            if self.config.research.arxiv_enabled and self.research_due("arxiv-theory"):
                entries.append(("arxiv-theory", 1.0, lambda: fetch_arxiv_theory(
                    client, self.config.research, self.config.lookback_hours)))
            total = 0
            for key, authority, fetch in entries:
                try:
                    items = await fetch()
                    with self.sessions.begin() as session:
                        state = session.get(SourceState, key)
                        if isinstance(items, XCollectionResult):
                            # X has already committed each page with its cursor.
                            # Never re-ingest it or advance a failed page here.
                            state.status, state.message = items.status, items.message
                            state.item_count = items.read_count
                            if items.committed_pages:
                                state.last_success_at = now_iso()
                            total += items.accepted_count
                            continue
                        if isinstance(items, ResearchResult):
                            count = ingest(session, items.items, self.config, authority)
                            state.status, state.message = items.status, items.message + f" 新增 {count} 篇。"
                            state.item_count = len(items.items)
                            if items.status == "healthy" or items.items:
                                state.last_success_at = now_iso()
                            total += count
                            continue
                        count = ingest(session, items, self.config, authority)
                        state.status, state.message = (
                            "healthy",
                            f"本轮读取 {len(items)} 条，新增有效信息 {count} 条。",
                        )
                        state.last_success_at, state.item_count = now_iso(), len(items)
                        total += count
                except Exception as exc:
                    with self.sessions.begin() as session:
                        state = session.get(SourceState, key)
                        partial_message = ""
                        if isinstance(exc, SourceUnavailable) and exc.partial_items:
                            count = ingest(session, exc.partial_items, self.config, authority)
                            total += count
                            state.last_success_at, state.item_count = now_iso(), len(exc.partial_items)
                            partial_message = (
                                f"本轮已读取 {len(exc.partial_items)} 条，已保留其中符合筛选条件的内容"
                                f"（新增 {count} 条）；采集尚未完成。"
                            )
                        state.status = exc.status if isinstance(exc, SourceUnavailable) else "error"
                        state.message = partial_message + (
                            exc.message
                            if isinstance(exc, SourceUnavailable)
                            else f"采集失败（{type(exc).__name__}），下轮自动重试。"
                        )
                    logger.warning("Source %s failed: %s", key, type(exc).__name__)
                finally:
                    with self.sessions.begin() as session:
                        session.get(SourceState, key).last_attempt_at = now_iso()
            return total

    async def digest(self, day: date, force=False, *, translate=True):
        start, end = digest_window(day, self.config)
        if end > datetime.now(UTC):
            raise ValueError("日报统计窗口尚未结束")
        with self.sessions() as session:
            existing = session.get(Digest, day.isoformat())
            if existing and not force:
                return existing.date
            expected_digest = as_dict(existing) if existing is not None else None
            selection = select_digest_articles(session, self.config, day, start, end)
            selected = selection.articles
            coverage = [as_dict(s) for s in session.scalars(select(SourceState))]
        if not selected:
            healthy = [s for s in coverage if s["status"] == "healthy"]
            if not healthy:
                raise ValueError("该统计窗口没有有效来源，未生成日报；请先完成采集或授权。")
            missing = [s["name"] for s in coverage if s["status"] != "healthy"]
            overview = (
                "已连接的来源在本次 24 小时统计窗口内，未发现符合筛选条件的新信息。旧消息仍可在雷达中回看。"
            )
            if missing:
                overview += "尚未完整覆盖：" + "、".join(missing) + "，因此这不代表全网没有 AI 动态。"
            with self.sessions.begin() as session:
                if not publication_is_current(session, day, force, expected_digest):
                    return day.isoformat()
                session.merge(
                    Digest(
                        date=day.isoformat(),
                        title="今天，保持关注与留白",
                        overview=overview,
                        stories=[],
                        provider="no_updates",
                        model=None,
                        window_start=start.isoformat(),
                        window_end=end.isoformat(),
                        source_count=0,
                        coverage=coverage,
                        generated_at=now_iso(),
                    )
                )
            return day.isoformat()
        if self.config.reading.enabled:
            selected = self.reading.evidence(selected)
        reviewed = self.config.summary_review.enabled and self.config.provider.kind != "extractive"
        if reviewed:
            with self.sessions() as session:
                evidence = digest_review_evidence(session, selected, self.config)
        selected = (await self.translations.evidence(selected) if translate else
                    await self.translations.evidence(selected, translate=False))
        provider = make_provider(self.config.provider)
        if reviewed:
            result = await self.summary_reviews.generate_digest(
                "digest:" + day.isoformat(), selected, day.isoformat(), provider, evidence=evidence,
            )
        else:
            # Explicit offline excerpt mode stays labelled as an excerpt; it is
            # never an automatic fallback when model review is unavailable.
            result = await provider.generate(selected, day.isoformat())
        with self.sessions.begin() as session:
            if not publication_is_current(session, day, force, expected_digest):
                return day.isoformat()
            if reviewed:
                current = digest_review_evidence(session, selected, self.config)
                if review_evidence_fingerprint(current) != review_evidence_fingerprint(evidence):
                    raise SummaryReviewPending("摘要来源已更新，等待服务器使用新证据审核。")
            session.merge(
                Digest(
                    date=day.isoformat(),
                    title=result.title,
                    overview=result.overview,
                    stories=mark_supplemental_stories(result.stories, selection),
                    provider=self.config.provider.kind,
                    model=self.config.provider.model,
                    window_start=start.isoformat(),
                    window_end=end.isoformat(),
                    source_count=len(selected),
                    coverage=coverage,
                    generated_at=now_iso(),
                )
            )
        return day.isoformat()

    def latest_day(self) -> date:
        now = datetime.now(ZoneInfo(self.config.timezone))
        return (
            (now - timedelta(days=1)).date()
            if (now.hour, now.minute) < (self.config.daily_hour, self.config.daily_minute)
            else now.date()
        )

    def has_pending(self, kind):
        if kind == "translate":
            return self.translations.has_pending()
        if kind == "read":
            return self.reading.has_pending() or self.presentations.has_pending()
        return False

    async def _managed_run(self, kind, day, force, uid, phase_callback):
        """Finish a bounded batch; services own the durable content checkpoints."""
        with self.sessions() as session:
            job = session.get(Job, uid)
            if not job or job.status != "running" or not job.owner:
                raise RuntimeError("任务尚未被执行器接管。")
            owner = job.owner
        lock = {"collect": self.collect_lock, "translate": self.translate_lock}.get(kind, self.lock)
        async with lock:
            message = ""
            if kind in ("collect", "daily"):
                await phase_callback("collect")
                if kind == "daily":
                    async with self.collect_lock:
                        count = await self.collect()
                else:
                    count = await self.collect()
                message = f"新增 {count} 条有效信息。"
            if kind == "translate" and self.config.translation.enabled:
                await phase_callback("translate")
                await self.translations.pending(force=force, limit=2, max_stage_calls=2)
                message = "本批翻译进度已保存，后续批次将自动继续。"
            if kind == "read":
                if self.config.reading.enabled:
                    await phase_callback("read")
                    self.reading.summary_reviews.stage_call_limit = 2
                    try:
                        reading = await self.reading.pending(force=force, translate=False, limit=1)
                    finally:
                        self.reading.summary_reviews.stage_call_limit = None
                    message = (f"本轮处理 {reading['fetched']} 个直接来源，"
                               f"完成 {reading['summarized']} 份网页解读。")
                await phase_callback("presentation")
                self.presentations.reviews.stage_call_limit = 2
                try:
                    await self.presentations.pending(limit=1)
                finally:
                    self.presentations.reviews.stage_call_limit = None
            if kind in ("digest", "daily"):
                await phase_callback("digest")
                edition = await self.digest(day or self.latest_day(), force, translate=False)
                message += f"已生成 {edition} 日报。"
            more_pending = self.has_pending(kind)
            with self.sessions.begin() as session:
                job = session.get(Job, uid)
                if not job or job.status != "running" or job.owner != owner:
                    raise RuntimeError("任务执行状态已变更。")
                job.status, job.message, job.finished_at = "completed", message, now_iso()
                job.more_pending = more_pending
            return uid

    async def run(self, kind="collect", day=None, force=False, job_id=None, phase_callback=None):
        if phase_callback is not None:
            return await self._managed_run(kind, day, force, job_id, phase_callback)
        async with self.lock:
            with self.sessions.begin() as session:
                job = session.get(Job, job_id) if job_id else Job(kind=kind)
                session.add(job)
                session.flush()
                uid = job.id
            try:
                message = ""
                if kind in ("collect", "daily"):
                    message = f"新增 {await self.collect()} 条有效信息。"
                if self.config.translation.enabled:
                    translated = await self.translations.pending(force=force if kind == "translate" else False)
                    counts = translated.get("counts", {})
                    message += f"主消息中文版本 {counts.get('ready', 0)} 条"
                    waiting = sum(n for status, n in counts.items() if status != "ready")
                    message += f"，{waiting} 条仍在等待翻译或校对。" if waiting else "。"
                    resources = translated.get("resource_counts", {})
                    message += f"网页正文中文版本 {resources.get('ready', 0)} 份"
                    resource_waiting = sum(n for status, n in resources.items() if status != "ready")
                    message += (
                        f"，{resource_waiting} 份仍在等待翻译或校对。" if resource_waiting else "。"
                    )
                if self.config.reading.enabled and kind != "translate":
                    reading = await self.reading.pending(force=force if kind == "read" else False)
                    message += f"本轮处理 {reading['fetched']} 个直接来源，完成 {reading['summarized']} 份网页解读。"
                if kind in ("collect", "read", "digest", "daily"):
                    await self.presentations.pending()
                if kind in ("digest", "daily"):
                    message += f"已生成 {await self.digest(day or self.latest_day(), force)} 日报。"
                with self.sessions.begin() as session:
                    job = session.get(Job, uid)
                    job.status, job.message, job.finished_at = "completed", message, now_iso()
            except Exception as exc:
                with self.sessions.begin() as session:
                    job = session.get(Job, uid)
                    job.status, job.finished_at = "failed", now_iso()
                    job.message = (
                        str(exc)
                        if isinstance(exc, ValueError) and len(str(exc)) < 160
                        else f"处理失败（{type(exc).__name__}），请检查模型登录、配置与来源状态。"
                    )
                logger.warning("Job %s failed: %s", uid, type(exc).__name__)
            return uid

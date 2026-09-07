import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from .config import RadarConfig
from .enrichment import enrich
from .models import Article, Digest, Job, SourceState, Watch, now_iso
from .providers import make_provider
from .ranking import article_id, canonicalize, classify, engagement, rank
from .reading import ReadingService, remember_references, sync_documents
from .schemas import IncomingArticle
from .sources import SourceUnavailable, fetch_anthropic, fetch_facebook, fetch_rss, fetch_x
from .translation import TranslationService, queue_article

logger = logging.getLogger(__name__)


def as_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def ingest(session, items: list[IncomingArticle], config: RadarConfig, authority: float = 0) -> int:
    handles = {w.handle.lower() for w in session.scalars(select(Watch).where(Watch.enabled.is_(True)))}
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=config.lookback_hours)
    accepted = 0
    for item in items:
        if not cutoff <= item.published_at <= now + timedelta(minutes=5):
            continue
        topics = classify(item)
        if not topics:
            continue
        priority = item.handle.lower() in handles or authority >= 1.5
        if (
            item.platform in ("x", "facebook")
            and not priority
            and engagement(item.metrics) < config.min_engagement
        ):
            continue
        uid = article_id(item)
        existing = session.get(Article, uid)
        values = item.model_dump(exclude={"published_at", "references"})
        values.update(
            id=uid,
            published_at=item.published_at.isoformat(),
            canonical_url=canonicalize(item.url),
            topics=topics,
            priority=priority,
            score=rank(item, priority, authority, now),
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
        remember_references(session, existing, [r.model_dump() for r in item.references])
        if config.reading.enabled:
            sync_documents(session, existing, config)
    return accepted


def digest_window(day: date, config: RadarConfig):
    # The edition is the 24 hours ending at configured local delivery time, including DST transitions.
    end = datetime.combine(day, time(config.daily_hour, config.daily_minute), ZoneInfo(config.timezone))
    start = end - timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


class Pipeline:
    def __init__(self, sessions, config: RadarConfig):
        self.sessions, self.config = sessions, config
        self.lock = asyncio.Lock()
        self.translations = TranslationService(sessions, config.translation)
        self.reading = ReadingService(sessions, config, self.translations)
        with sessions.begin() as session:
            sources = [("x", "X / Twitter", "x"), ("facebook", "Facebook", "facebook")]
            sources += [(f.id, f.name, "rss") for f in config.feeds]
            if config.anthropic_news_enabled:
                sources.append(("anthropic", "Anthropic News", "web"))
            for key, name, platform in sources:
                if not session.get(SourceState, key):
                    session.add(SourceState(id=key, name=name, platform=platform))
            # One worker is required. Jobs interrupted by a restart are visibly failed, not left running.
            for job in session.scalars(select(Job).where(Job.status == "running")):
                job.status, job.message, job.finished_at = (
                    "failed",
                    "服务重启中断任务，可以重新运行。",
                    now_iso(),
                )
            for article in session.scalars(select(Article)):
                queue_article(session, article, config.translation)

    async def collect(self):
        with self.sessions() as session:
            handles = list(session.scalars(select(Watch.handle).where(Watch.enabled.is_(True))))
        async with httpx.AsyncClient(
            timeout=25, headers={"User-Agent": "AIRadar/0.1 (+personal intelligence reader)"}
        ) as client:
            entries = [
                ("x", 0, lambda: fetch_x(client, self.config, handles)),
                ("facebook", 0, lambda: fetch_facebook(client, self.config)),
            ]
            entries += [
                (f.id, f.authority, lambda feed=f: fetch_rss(client, feed)) for f in self.config.feeds
            ]
            if self.config.anthropic_news_enabled:
                entries.append(("anthropic", 2.0, lambda: fetch_anthropic(client)))
            total = 0
            for key, authority, fetch in entries:
                try:
                    items = await fetch()
                    with self.sessions.begin() as session:
                        count = ingest(session, items, self.config, authority)
                        state = session.get(SourceState, key)
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

    async def digest(self, day: date, force=False):
        start, end = digest_window(day, self.config)
        if end > datetime.now(UTC):
            raise ValueError("日报统计窗口尚未结束")
        with self.sessions() as session:
            existing = session.get(Digest, day.isoformat())
            if existing and existing.source_count > 0 and not force:
                return existing.date
            rows = session.scalars(
                select(Article)
                .where(Article.published_at >= start.isoformat(), Article.published_at < end.isoformat())
                .order_by(Article.score.desc())
            ).all()
            selected, seen = [], set()
            for row in rows:
                if row.canonical_url in seen:
                    continue
                seen.add(row.canonical_url)
                selected.append(
                    {
                        k: v
                        for k, v in as_dict(row).items()
                        if k
                        in {
                            "id",
                            "title",
                            "text",
                            "url",
                            "author",
                            "published_at",
                            "published_precision",
                            "topics",
                            "platform",
                            "metrics",
                        }
                    }
                )
                if len(selected) == self.config.provider.max_items:
                    break
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
        elif self.config.enrich_official_articles:
            selected = await enrich(selected)
        selected = await self.translations.evidence(selected)
        result = await make_provider(self.config.provider).generate(selected, day.isoformat())
        with self.sessions.begin() as session:
            session.merge(
                Digest(
                    date=day.isoformat(),
                    title=result.title,
                    overview=result.overview,
                    stories=[s.model_dump() for s in result.stories],
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

    async def run(self, kind="collect", day=None, force=False, job_id=None):
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

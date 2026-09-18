import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import case, func, select

from . import usage
from .account_status import AccountMonitor
from .admission import status as admission_status
from .article_filters import ArticleTopic, article_query, author_options
from .chat import ChatService
from .chat_api import mount_chat
from .config import Settings
from .daily_schedule import DAILY_CHECK_MINUTES, DailySchedule
from .db import database
from .discovery_watches import discovery_status, list_entities, on_watch_toggle, watch_metadata
from .freshness import freshness_status, translation_updates
from .jobs import JobQueueConflict, JobSupervisor, job_counts, public_job
from .market_api import mount_market
from .models import Article, Digest, Job, SourceState, Watch
from .pipeline import Pipeline, as_dict, ingest
from .schemas import Bookmark, ImportBatch, Toggle, WatchInput
from .translation import present_articles, translation_status
from .x_costs import XCostLedger


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    config = settings.load()
    engine, sessions = database(settings.database_url)
    pipeline = Pipeline(sessions, config)
    supervisor = JobSupervisor(pipeline, automatic=settings.scheduler_enabled)
    daily = DailySchedule(pipeline, submit=supervisor.submit)
    accounts = AccountMonitor(settings.accounts_config_path, settings.accounts_database_path)

    chat = ChatService(pipeline, settings)

    async def poll_accounts():
        accounts.kick()

    @asynccontextmanager
    async def lifespan(_app):
        scheduler = AsyncIOScheduler(timezone=config.timezone)
        await supervisor.start()
        await chat.start()
        if accounts.enabled:
            accounts.kick()
            scheduler.add_job(poll_accounts, "interval", seconds=30, id="account_status", max_instances=1,
                coalesce=True)
        if settings.scheduler_enabled:
            scheduler.add_job(
                supervisor.schedule_collect,
                "interval",
                minutes=config.collect_minutes,
                id="collect",
                max_instances=1,
                coalesce=True,
            )
            scheduler.add_job(
                daily.run,
                "cron",
                hour=config.daily_hour,
                minute=config.daily_minute,
                id="daily",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            scheduler.add_job(
                daily.run,
                "interval",
                minutes=DAILY_CHECK_MINUTES,
                next_run_time=datetime.now(UTC),
                id="daily_recovery",
                max_instances=1,
                coalesce=True,
            )
        if settings.scheduler_enabled or accounts.enabled:
            scheduler.start()
        yield
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await chat.stop()
        await supervisor.stop()
        await accounts.close()
        engine.dispose()

    app = FastAPI(title="AI Radar", version="0.1.0", lifespan=lifespan)
    app.state.sessions, app.state.pipeline = sessions, pipeline
    app.state.supervisor = supervisor
    app.state.accounts = accounts
    app.state.chat = chat
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
    )
    bearer = HTTPBearer(auto_error=False)

    def authenticated(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        value = credentials.credentials if credentials else ""
        allowed = [settings.reader_token, settings.admin_token]
        if not value or not any(token and secrets.compare_digest(value, token) for token in allowed):
            raise HTTPException(401, "请输入有效的设备访问令牌", headers={"WWW-Authenticate": "Bearer"})
        return value

    def admin(value: str = Depends(authenticated)):
        if not settings.admin_token or not secrets.compare_digest(value, settings.admin_token):
            raise HTTPException(403, "此操作需要管理令牌")

    def session_dep():
        with sessions() as session:
            yield session

    def enqueue_reading():
        return supervisor.submit("read")

    from .browser_api import mount_browser
    mount_browser(app, settings, sessions, authenticated, admin, enqueue_reading)
    from .industry_api import mount_industry
    mount_industry(app, pipeline, supervisor, authenticated)
    mount_chat(app, chat, authenticated, admin)
    from .companion import mount_companion
    mount_companion(app, settings, sessions, authenticated, enqueue_reading)
    mount_market(app, settings, authenticated)

    @app.get("/healthz")
    def health(session=Depends(session_dep)):
        session.execute(select(1))
        return {"status": "ok", "service": "ai-radar"}

    @app.get("/v1/usage", dependencies=[Depends(authenticated)])
    def model_usage(period: Literal["today", "7d", "30d", "all"] = "7d"):
        try:
            ledger = usage.store(settings.usage_database_path)
            result = ledger.report(period, config.timezone) if ledger else {"enabled": False, "available": False}
        except (OSError, sqlite3.Error):
            result = {"enabled": True, "available": False, "error": "用量记录暂时无法读取，请稍后重试。"}
        return {**result, "allocation": usage.allocation(config),
            "features": usage.FEATURES, "stages": usage.STAGES}

    @app.get("/v1/accounts", dependencies=[Depends(authenticated)])
    def account_status():
        return accounts.report()

    @app.post("/v1/accounts/refresh", dependencies=[Depends(authenticated)])
    async def refresh_accounts():
        # This refreshes read-only account data; it never enqueues inference or payment.
        accounts.kick(force=True)
        return accounts.report()

    @app.get("/v1/status", dependencies=[Depends(authenticated)])
    def status(session=Depends(session_dep)):
        return {
            "server_now": datetime.now(UTC).isoformat(),
            "timezone": config.timezone,
            "daily_time": f"{config.daily_hour:02}:{config.daily_minute:02}",
            "provider": config.provider.kind,
            "model": config.provider.model,
            "scheduler_enabled": settings.scheduler_enabled,
            "freshness": freshness_status(session, config),
            "x_data": XCostLedger(sessions, config).report(),
            "article_count": session.scalar(select(func.count()).select_from(Article)),
            "admission": admission_status(session),
            "translation": translation_status(session, config.translation),
            "discovery": discovery_status(session, config),
            "sources": [as_dict(s) for s in session.scalars(select(SourceState))],
            "jobs": [
                public_job(j) for j in session.scalars(select(Job).order_by(
                    case((Job.status.in_(("running", "queued", "retrying")), 0), else_=1),
                    Job.started_at.desc(),
                ).limit(10))
            ],
            "job_counts": job_counts(session),
        }

    @app.get("/v1/articles", dependencies=[Depends(authenticated)])
    def articles(
        q: str = Query(default="", max_length=200),
        platform: str | None = None,
        topic: ArticleTopic | None = None,
        author: str = Query(default="", max_length=440),
        saved: bool = False,
        priority: bool = False,
        sort: Literal["score", "latest"] = "latest",
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10000),
        session=Depends(session_dep),
    ):
        try:
            query = article_query(q=q, platform=platform, topic=topic, saved=saved,
                                  priority=priority, author=author)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        query = (
            query.order_by(Article.score.desc(), Article.id)
            if sort == "score"
            else query.order_by(Article.published_at.desc(), Article.id)
        )
        return {
            "items": present_articles(session, session.scalars(query.limit(limit).offset(offset)),
                                      config.translation, presentation_config=config),
            "total": total,
        }

    @app.get("/v1/article-authors", dependencies=[Depends(authenticated)])
    def article_authors(
        q: str = Query(default="", max_length=200),
        platform: str | None = None,
        topic: ArticleTopic | None = None,
        saved: bool = False,
        priority: bool = False,
        session=Depends(session_dep),
    ):
        items = author_options(session, article_query(
            q=q, platform=platform, topic=topic, saved=saved, priority=priority,
        ))
        return {"items": items, "total": sum(item["count"] for item in items)}

    @app.post("/v1/refresh", dependencies=[Depends(authenticated)])
    async def refresh_latest():
        return supervisor.refresh_latest()

    @app.get("/v1/translation-updates", dependencies=[Depends(authenticated)])
    def completed_translations(limit: int = Query(default=50, ge=1, le=200), session=Depends(session_dep)):
        return translation_updates(session, config, limit=limit)

    @app.get("/v1/articles/{uid}", dependencies=[Depends(authenticated)])
    def article(uid: str, session=Depends(session_dep)):
        row = session.get(Article, uid)
        if not row:
            raise HTTPException(404, "文章不存在")
        return present_articles(session, [row], config.translation, full_resources=True, presentation_config=config)[0]

    @app.put("/v1/articles/{uid}/bookmark", dependencies=[Depends(authenticated)])
    def bookmark(uid: str, body: Bookmark, session=Depends(session_dep)):
        row = session.get(Article, uid)
        if not row:
            raise HTTPException(404, "文章不存在")
        row.saved = body.saved
        session.commit()
        return {"saved": row.saved}

    @app.get("/v1/digests", dependencies=[Depends(authenticated)])
    def digests(limit: int = Query(default=30, ge=1, le=100), session=Depends(session_dep)):
        return {
            "items": [
                as_dict(d) for d in session.scalars(select(Digest).order_by(Digest.date.desc()).limit(limit))
            ]
        }

    @app.get("/v1/digests/{day}", dependencies=[Depends(authenticated)])
    def digest(day: str, session=Depends(session_dep)):
        row = (
            session.scalar(select(Digest).order_by(Digest.date.desc()).limit(1))
            if day == "latest"
            else session.get(Digest, day)
        )
        if not row:
            raise HTTPException(404, "日报尚未生成")
        data = as_dict(row)
        ids = {uid for story in row.stories for uid in story["source_ids"]}
        data["sources"] = present_articles(session, session.scalars(select(Article).where(Article.id.in_(ids))),
                                          config.translation, presentation_config=config)
        return data

    @app.get("/v1/watches", dependencies=[Depends(authenticated)])
    def watches(session=Depends(session_dep)):
        return {"items": [{**as_dict(w), **({"discovery": metadata} if (metadata := watch_metadata(session, w)) else {})}
                          for w in session.scalars(select(Watch))]}

    @app.get("/v1/discovery/entities", dependencies=[Depends(authenticated)])
    def discovery_entities(limit: int = Query(default=50, ge=1, le=100), session=Depends(session_dep)):
        return {"items": list_entities(session, limit=limit)}

    @app.post("/v1/watches", dependencies=[Depends(authenticated)])
    def add_watch(body: WatchInput, session=Depends(session_dep)):
        if session.scalar(select(func.count()).select_from(Watch)) >= 100:
            raise HTTPException(409, "当前版本最多维护 100 个重点账号")
        uid = "x:" + body.handle.lower()
        if session.get(Watch, uid):
            raise HTTPException(409, "该账号已在关注名单")
        row = Watch(id=uid, **body.model_dump())
        session.add(row)
        session.commit()
        return as_dict(row)

    @app.patch("/v1/watches/{uid}", dependencies=[Depends(authenticated)])
    def toggle_watch(uid: str, body: Toggle, session=Depends(session_dep)):
        row = session.get(Watch, uid)
        if not row:
            raise HTTPException(404, "账号不存在")
        on_watch_toggle(session, row, body.enabled)
        row.enabled = body.enabled
        session.commit()
        return as_dict(row)

    @app.post("/v1/admin/import", dependencies=[Depends(admin)])
    async def import_articles(body: ImportBatch, session=Depends(session_dep)):
        count = ingest(session, body.articles, config)
        session.commit()
        if config.translation.enabled:
            supervisor.submit("translate")
        if config.reading.enabled:
            supervisor.submit("read")
        if pipeline.discovery.has_pending():
            supervisor.submit("discover")
        return {"accepted": count, "received": len(body.articles)}

    @app.post("/v1/admin/jobs", status_code=202, dependencies=[Depends(admin)])
    async def start_job(
        kind: Literal["collect", "digest", "daily", "translate", "read", "discover"] = "daily",
        day: date | None = None,
        force: bool = False,
        session=Depends(session_dep),
    ):
        try:
            uid = supervisor.submit(kind, day=day, force=force)
        except JobQueueConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return {"job_id": uid}

    return app

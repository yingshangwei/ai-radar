import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, or_, select

from .config import Settings
from .db import database
from .models import Article, ArticleTranslation, Digest, Job, SourceState, Translation, Watch
from .pipeline import Pipeline, as_dict, ingest
from .schemas import Bookmark, ImportBatch, Toggle, WatchInput
from .translation import present_articles, translation_status


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    config = settings.load()
    engine, sessions = database(settings.database_url)
    pipeline = Pipeline(sessions, config)
    tasks = set()

    @asynccontextmanager
    async def lifespan(_app):
        scheduler = AsyncIOScheduler(timezone=config.timezone)
        if settings.scheduler_enabled:
            scheduler.add_job(
                pipeline.run,
                "interval",
                minutes=config.collect_minutes,
                kwargs={"kind": "collect"},
                id="collect",
                max_instances=1,
                coalesce=True,
            )
            scheduler.add_job(
                pipeline.run,
                "cron",
                hour=config.daily_hour,
                minute=config.daily_minute,
                kwargs={"kind": "daily"},
                id="daily",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            scheduler.start()
        yield
        if scheduler.running:
            scheduler.shutdown(wait=False)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        engine.dispose()

    app = FastAPI(title="AI Radar", version="0.1.0", lifespan=lifespan)
    app.state.sessions, app.state.pipeline = sessions, pipeline
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

    @app.get("/healthz")
    def health(session=Depends(session_dep)):
        session.execute(select(1))
        return {"status": "ok", "service": "ai-radar"}

    @app.get("/v1/status", dependencies=[Depends(authenticated)])
    def status(session=Depends(session_dep)):
        return {
            "timezone": config.timezone,
            "daily_time": f"{config.daily_hour:02}:{config.daily_minute:02}",
            "provider": config.provider.kind,
            "model": config.provider.model,
            "scheduler_enabled": settings.scheduler_enabled,
            "article_count": session.scalar(select(func.count()).select_from(Article)),
            "translation": translation_status(session, config.translation),
            "sources": [as_dict(s) for s in session.scalars(select(SourceState))],
            "jobs": [
                as_dict(j) for j in session.scalars(select(Job).order_by(Job.started_at.desc()).limit(10))
            ],
        }

    @app.get("/v1/articles", dependencies=[Depends(authenticated)])
    def articles(
        q: str = Query(default="", max_length=200),
        platform: str | None = None,
        topic: Literal["模型", "产品", "技术", "开源", "观点", "产业"] | None = None,
        saved: bool = False,
        priority: bool = False,
        sort: Literal["score", "latest"] = "score",
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10000),
        session=Depends(session_dep),
    ):
        query = select(Article)
        if not saved and not q:
            query = query.where(Article.published_at >= (datetime.now(UTC) - timedelta(days=7)).isoformat())
        if q:
            chinese_matches = select(ArticleTranslation.article_id).join(
                Translation, ArticleTranslation.translation_id == Translation.id
            ).where(Translation.status == "ready", or_(
                Translation.title_zh.contains(q, autoescape=True),
                Translation.text_zh.contains(q, autoescape=True),
            ))
            query = query.where(
                or_(
                    Article.title.contains(q, autoescape=True),
                    Article.text.contains(q, autoescape=True),
                    Article.author.contains(q, autoescape=True),
                    Article.id.in_(chinese_matches),
                )
            )
        if platform:
            query = query.where(Article.platform == platform)
        if topic:
            query = query.where(Article.topics.contains(topic))
        if saved:
            query = query.where(Article.saved.is_(True))
        if priority:
            query = query.where(Article.priority.is_(True))
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        query = (
            query.order_by(Article.score.desc(), Article.id)
            if sort == "score"
            else query.order_by(Article.published_at.desc(), Article.id)
        )
        return {
            "items": present_articles(session, session.scalars(query.limit(limit).offset(offset)),
                                      config.translation),
            "total": total,
        }

    @app.get("/v1/articles/{uid}", dependencies=[Depends(authenticated)])
    def article(uid: str, session=Depends(session_dep)):
        row = session.get(Article, uid)
        if not row:
            raise HTTPException(404, "文章不存在")
        return present_articles(session, [row], config.translation)[0]

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
                                          config.translation)
        return data

    @app.get("/v1/watches", dependencies=[Depends(authenticated)])
    def watches(session=Depends(session_dep)):
        return {"items": [as_dict(w) for w in session.scalars(select(Watch))]}

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
        row.enabled = body.enabled
        session.commit()
        return as_dict(row)

    @app.post("/v1/admin/import", dependencies=[Depends(admin)])
    async def import_articles(body: ImportBatch, session=Depends(session_dep)):
        count = ingest(session, body.articles, config)
        session.commit()
        if config.translation.enabled and not pipeline.lock.locked() and not tasks:
            job = Job(kind="translate")
            session.add(job)
            session.commit()
            task = asyncio.create_task(pipeline.run(kind="translate", job_id=job.id))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return {"accepted": count, "received": len(body.articles)}

    @app.post("/v1/admin/jobs", status_code=202, dependencies=[Depends(admin)])
    async def start_job(
        kind: Literal["collect", "digest", "daily", "translate"] = "daily",
        day: date | None = None,
        force: bool = False,
        session=Depends(session_dep),
    ):
        if pipeline.lock.locked() or any(not task.done() for task in tasks):
            raise HTTPException(409, "已有任务执行中")
        job = Job(kind=kind)
        session.add(job)
        session.commit()
        task = asyncio.create_task(pipeline.run(kind=kind, day=day, force=force, job_id=job.id))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"job_id": job.id}

    return app

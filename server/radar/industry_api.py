"""Reader-scoped industry routes. Refresh cannot invoke paid news or force models."""

from datetime import timedelta

from fastapi import Depends, HTTPException, Query
from sqlalchemy import select

from .industry import public_evidence, utc
from .industry_config import THEMES
from .industry_models import IndustryEvidence, IndustryTracking
from .jobs import public_job
from .models import Job
from .schemas import Toggle


def mount_industry(app, pipeline, supervisor, authenticated):
    service, sessions = pipeline.industry, pipeline.sessions

    @app.get("/v1/industry", dependencies=[Depends(authenticated)])
    def overview():
        result = service.overview()
        with sessions() as session:
            last = session.scalar(select(Job).where(Job.kind == "industry_collect").order_by(
                Job.started_at.desc()).limit(1))
            result["latest_job"] = public_job(last) if last else None
        return result

    @app.get("/v1/industry/evidence", dependencies=[Depends(authenticated)])
    def evidence(theme: str | None = None, q: str = Query(default="", max_length=200),
                 limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000)):
        if theme is not None and theme not in THEMES:
            raise HTTPException(404, "行业主题不存在")
        return service.evidence(theme, q, limit, offset)

    @app.get("/v1/industry/evidence/{uid}", dependencies=[Depends(authenticated)])
    def evidence_version(uid: str):
        with sessions() as session:
            row = session.get(IndustryEvidence, uid)
            if row is None:
                raise HTTPException(404, "证据版本不存在")
            return public_evidence(row)

    @app.get("/v1/industry/jobs/{uid}", dependencies=[Depends(authenticated)])
    def job_status(uid: str):
        with sessions() as session:
            row = session.get(Job, uid)
            if row is None or row.kind not in ("industry_collect", "industry_analyze"):
                raise HTTPException(404, "行业任务不存在")
            return public_job(row)

    @app.post("/v1/industry/refresh", dependencies=[Depends(authenticated)])
    async def refresh():
        if not service.options.enabled:
            raise HTTPException(409, "服务端尚未启用行业研究")
        with sessions() as session:
            if not service._enabled_themes(session):
                raise HTTPException(409, "请先开启至少一个行业主题")
            active = session.scalar(select(Job).where(
                Job.kind == "industry_collect", Job.status.in_(("queued", "running", "retrying")),
            ).order_by(Job.started_at.desc()).limit(1))
            last = active or session.scalar(select(Job).where(Job.kind == "industry_collect").order_by(
                Job.started_at.desc()).limit(1))
            if active or last and service.clock() - utc(last.finished_at or last.started_at) < timedelta(seconds=120):
                return public_job(last)
        uid = supervisor.submit("industry_collect")
        with sessions() as session:
            return public_job(session.get(Job, uid))

    @app.put("/v1/industry/themes/{theme_id}/tracking", dependencies=[Depends(authenticated)])
    def track(theme_id: str, body: Toggle):
        if theme_id not in THEMES:
            raise HTTPException(404, "行业主题不存在")
        if not service.options.enabled:
            raise HTTPException(409, "服务端尚未启用行业研究")
        with service.transaction() as session:
            row = session.get(IndustryTracking, theme_id)
            row.enabled, row.updated_at = body.enabled, service.clock().isoformat()
        return {"id": theme_id, "enabled": body.enabled}

"""A scoped Mac collector credential cannot call admin or Chat APIs."""
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now_iso


class CompanionDevice(Base):
    __tablename__ = "companion_devices"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    seen_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    report: Mapped[dict] = mapped_column(JSON, default=dict)


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    state: str = Field(pattern=r"^(ready|working|paused|verification|error)$")
    saved: int = Field(default=0, ge=0)
    domains: list[str] = Field(default_factory=list, max_length=30)
    waiting: int = Field(default=0, ge=0, le=100)


def mount_companion(app, settings, sessions, authenticated, enqueue):
    bearer = HTTPBearer(auto_error=False)

    def collector(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        token = credentials.credentials if credentials else ""
        if not settings.companion_token or not secrets.compare_digest(token, settings.companion_token):
            raise HTTPException(401, "Mac 补采凭据无效")

    router = APIRouter(prefix="/v1/companion")
    from .mobile_capture import mount_mobile_capture
    from .mobile_queue import mount_mobile_queue, permitted_domains
    mount_mobile_queue(router, sessions, collector)
    mount_mobile_capture(router, sessions, collector, enqueue)

    @router.post("/heartbeat", dependencies=[Depends(collector)])
    def heartbeat(body: Report):
        permitted_domains(",".join(body.domains))
        with sessions.begin() as db:
            row = db.get(CompanionDevice, body.device)
            if row is None:
                row = CompanionDevice(id=body.device)
                db.add(row)
            row.seen_at, row.report = now_iso(), body.model_dump(exclude={"device"})
        return {"ok": True}

    @router.get("/status", dependencies=[Depends(authenticated)])
    def status():
        with sessions() as db:
            rows = db.scalars(select(CompanionDevice).order_by(CompanionDevice.seen_at.desc()).limit(5))
            return {"configured": bool(settings.companion_token), "devices": [
                {"id": row.id, "seen_at": row.seen_at,
                 "online": (datetime.now(UTC) - datetime.fromisoformat(row.seen_at)).total_seconds() < 180,
                 **row.report} for row in rows]}

    app.include_router(router)

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .chat import ChatError, public_session, public_turn
from .chat_models import ChatSession, ChatTurn


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{12,80}$")
    question: str = Field(min_length=1, max_length=6000)
    profile: str = Field(pattern=r"^(standard|confirmation|adjudication)$", default="standard")


def mount_chat(app, service, authenticated, admin=None):
    # Privileged chat includes command output/history: protect every endpoint.
    access = admin if service.options.agent_enabled else authenticated
    if access is None:
        raise ValueError("Agent chat requires administrator authentication")
    @app.get("/v1/chat", dependencies=[Depends(access)])
    def overview():
        worker_online = True
        if service.options.external_worker:
            try:
                heartbeat = json.loads((Path(service.options.state_directory) / "heartbeat.json").read_text())
                worker_online = (datetime.now(UTC) - datetime.fromisoformat(heartbeat["at"])).total_seconds() < 45
            except (OSError, ValueError, KeyError):
                worker_online = False
        with service.sessions() as db:
            rows = list(db.scalars(select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(100)))
            return {
                "enabled": service.available(),
                "agent_enabled": service.options.agent_enabled,
                "worker_online": worker_online,
                "models": service.models(),
                "sessions": [public_session(s) for s in rows],
                "max_pending": service.options.max_pending,
            }

    @app.post("/v1/chat/sessions", dependencies=[Depends(access)])
    def create_session():
        if not service.available():
            raise HTTPException(409, "服务器尚未启用 Codex 对话")
        with service.transaction() as db:
            # Empty sessions are reused so a lost creation response does not create duplicates.
            row = db.scalar(
                select(ChatSession)
                .where(~select(ChatTurn.id).where(ChatTurn.session_id == ChatSession.id).exists())
                .order_by(ChatSession.created_at.desc())
                .limit(1)
            )
            if not row:
                row = ChatSession(id=str(uuid4()))
                db.add(row)
                db.flush()
            return public_session(row)

    @app.get("/v1/chat/sessions/{sid}", dependencies=[Depends(access)])
    def read_session(sid: str):
        with service.sessions() as db:
            row = db.get(ChatSession, sid)
            if row is None:
                raise HTTPException(404, "会话不存在")
            turns = list(
                db.scalars(
                    select(ChatTurn)
                    .where(ChatTurn.session_id == sid)
                    .order_by(ChatTurn.created_at.desc(), ChatTurn.id.desc())
                    .limit(100)
                )
            )
            return {
                **public_session(row),
                "messages": [public_turn(t) for t in reversed(turns)],
                "history_limit": 100,
            }

    @app.post("/v1/chat/sessions/{sid}/messages", dependencies=[Depends(access)])
    def send_message(sid: str, body: Message):
        if not body.question.strip():
            raise HTTPException(422, "请输入消息")
        try:
            return service.enqueue(sid, body.id, body.question.strip(), body.profile)
        except ChatError as exc:
            raise HTTPException(409, str(exc)) from None

"""Durable user conversations, isolated from automated model sessions."""

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now_iso


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(100), default="新对话")
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    profile: Mapped[str] = mapped_column(String(30), default="standard")
    cli_session: Mapped[str] = mapped_column(String(40), default="")
    provider_hash: Mapped[str] = mapped_column(String(64), default="")
    generation: Mapped[int] = mapped_column(Integer, default=0)
    turns: Mapped[int] = mapped_column(Integer, default=0)


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    question: Mapped[str] = mapped_column(Text)
    profile: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    answer: Mapped[str] = mapped_column(Text, default="")
    references: Mapped[list] = mapped_column(JSON, default=list)
    tokens: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso, index=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    context_at: Mapped[str] = mapped_column(String(40), default="")
    failure: Mapped[str] = mapped_column(String(100), default="")
    transport: Mapped[dict] = mapped_column(JSON, default=dict)

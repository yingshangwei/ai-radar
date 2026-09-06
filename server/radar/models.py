from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), index=True)
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, index=True)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(200))
    handle: Mapped[str] = mapped_column(String(200), default="")
    published_at: Mapped[str] = mapped_column(String(40), index=True)
    published_precision: Mapped[str] = mapped_column(String(20), default="timestamp")
    collected_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    score: Mapped[float] = mapped_column(Float, default=0, index=True)
    priority: Mapped[bool] = mapped_column(Boolean, default=False)
    saved: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("platform", "external_id"),)


class Watch(Base):
    __tablename__ = "watches"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    handle: Mapped[str] = mapped_column(String(120))
    platform: Mapped[str] = mapped_column(String(30), default="x")
    organization: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(120), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Translation(Base):
    """Content-addressed, durable translations; independent of mutable social metrics."""
    __tablename__ = "translations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    original_title: Mapped[str] = mapped_column(Text)
    original_text: Mapped[str] = mapped_column(Text)
    title_zh: Mapped[str] = mapped_column(Text, default="")
    text_zh: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    parts: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(100), default="")
    review_model: Mapped[str] = mapped_column(String(100), default="")
    revision: Mapped[str] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_at: Mapped[str] = mapped_column(String(40), default="")
    lease_until: Mapped[str] = mapped_column(String(40), default="")
    owner: Mapped[str] = mapped_column(String(36), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class ArticleTranslation(Base):
    __tablename__ = "article_translations"
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    translation_id: Mapped[str] = mapped_column(ForeignKey("translations.id"), index=True)


class TranslationAccountState(Base):
    __tablename__ = "translation_account_states"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(40), default="")
    observed_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class SourceState(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    message: Mapped[str] = mapped_column(Text, default="尚未采集")
    last_attempt_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_success_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)


class Digest(Base):
    __tablename__ = "digests"
    date: Mapped[str] = mapped_column(String(10), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    overview: Mapped[str] = mapped_column(Text)
    stories: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    generated_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    window_start: Mapped[str] = mapped_column(String(40))
    window_end: Mapped[str] = mapped_column(String(40))
    source_count: Mapped[int] = mapped_column(Integer)
    coverage: Mapped[list] = mapped_column(JSON, default=list)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="running")
    started_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")

"""Append-only evidence versions and model receipts, separate from AI news content."""

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now_iso


class IndustryEvidence(Base):
    __tablename__ = "industry_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_key: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    source_name: Mapped[str] = mapped_column(String(200))
    origin: Mapped[str] = mapped_column(String(200), index=True)
    external_id: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    published_at: Mapped[str] = mapped_column(String(40), index=True)
    published_precision: Mapped[str] = mapped_column(String(20), default="timestamp")
    first_seen_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    kind: Mapped[str] = mapped_column(String(40))
    entity_ids: Mapped[list] = mapped_column(JSON, default=list)
    theme_ids: Mapped[list] = mapped_column(JSON, default=list)
    partial: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class IndustryTracking(Base):
    __tablename__ = "industry_tracking"
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class IndustryAssessment(Base):
    __tablename__ = "industry_assessments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    theme_id: Mapped[str] = mapped_column(String(50), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    stage: Mapped[str] = mapped_column(String(30), default="generation")
    as_of: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso, index=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    completed_at: Mapped[str] = mapped_column(String(40), default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    audit: Mapped[dict] = mapped_column(JSON, default=dict)
    history: Mapped[list] = mapped_column(JSON, default=list)
    failure_code: Mapped[str] = mapped_column(String(80), default="")
    corrections: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str] = mapped_column(String(50), default="")
    lease_until: Mapped[str] = mapped_column(String(40), default="")

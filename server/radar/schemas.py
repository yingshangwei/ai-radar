from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DirectReference(BaseModel):
    url: str = Field(min_length=1, max_length=4000)
    label: str = Field(default="", max_length=300)
    short_url: str = Field(default="", max_length=4000)


class IncomingArticle(BaseModel):
    platform: Literal["x", "facebook", "rss", "web"]
    external_id: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=4000)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=30000)
    author: str = Field(min_length=1, max_length=200)
    handle: str = Field(default="", max_length=200)
    published_at: datetime
    published_precision: Literal["timestamp", "date"] = "timestamp"
    metrics: dict[str, int] = Field(default_factory=dict)
    source_id: str = Field(default="import", max_length=100)
    references: list[DirectReference] = Field(default_factory=list, max_length=30)

    @field_validator("url")
    @classmethod
    def public_link(cls, value):
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("A public http(s) source URL is required")
        return value

    @field_validator("published_at")
    @classmethod
    def aware_date(cls, value):
        if value.tzinfo is None:
            raise ValueError("published_at requires an explicit timezone")
        return value.astimezone(UTC)

    @field_validator("metrics")
    @classmethod
    def valid_metrics(cls, value):
        if len(value) > 20 or any(v < 0 for v in value.values()):
            raise ValueError("Metrics must be nonnegative")
        return value


class Story(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=1600)
    why_it_matters: str = Field(min_length=1, max_length=800)
    category: Literal["模型", "产品", "技术", "开源", "观点", "产业"]
    source_ids: list[str] = Field(min_length=1, max_length=12)


class DigestOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    overview: str = Field(min_length=1, max_length=2000)
    stories: list[Story] = Field(min_length=1, max_length=12)


class DocumentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    title_zh: str = Field(min_length=1, max_length=200)
    summary_zh: str = Field(min_length=1, max_length=1800)
    key_points_zh: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(min_length=1, max_length=6)
    why_it_matters_zh: str = Field(min_length=1, max_length=800)


class ReadingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    documents: list[DocumentSummary] = Field(min_length=1, max_length=8)


class ImportBatch(BaseModel):
    articles: list[IncomingArticle] = Field(min_length=1, max_length=200)


class Toggle(BaseModel):
    enabled: bool


class Bookmark(BaseModel):
    saved: bool


class WatchInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    handle: str = Field(pattern=r"^[A-Za-z0-9_]{1,15}$")
    organization: str = Field(default="", max_length=120)
    role: str = Field(default="自定义关注", max_length=120)

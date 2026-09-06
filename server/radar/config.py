import os
import tomllib
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderConfig(BaseModel):
    kind: Literal["codex", "claude_cli", "command", "openai", "openai_chat", "anthropic", "extractive"] = (
        "codex"
    )
    structured_outputs: bool = True
    model: str | None = None
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    command: list[str] = Field(default_factory=lambda: ["codex"])
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    max_items: int = Field(default=35, ge=1, le=100)
    env_allowlist: list[str] = Field(default_factory=list)


class FeedConfig(BaseModel):
    id: str
    name: str
    url: str
    authority: float = Field(default=1.0, ge=0, le=3)


class RadarConfig(BaseModel):
    timezone: str = "Asia/Shanghai"
    daily_hour: int = Field(default=8, ge=0, le=23)
    daily_minute: int = Field(default=0, ge=0, le=59)
    collect_minutes: int = Field(default=120, ge=15)
    lookback_hours: int = Field(default=168, ge=1, le=168)
    anthropic_news_enabled: bool = True
    enrich_official_articles: bool = True
    min_engagement: int = Field(default=30, ge=0)
    x_max_pages: int = Field(default=2, ge=1, le=10)
    x_query: str = '(AI OR "artificial intelligence" OR LLM OR agents OR robotics) -is:retweet'
    facebook_version: str = "v23.0"
    facebook_page_ids: list[str] = Field(default_factory=list)
    feeds: list[FeedConfig] = Field(default_factory=list)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str):
        ZoneInfo(value)
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RADAR_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/radar.db"
    config_path: str = "config.toml"
    reader_token: str = ""
    admin_token: str = ""
    scheduler_enabled: bool = False
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:8081", "http://localhost:8082"]
    )

    def load(self) -> RadarConfig:
        path = Path(self.config_path)
        if not path.is_file():
            raise RuntimeError(f"Configuration file not found: {path}")
        return RadarConfig.model_validate(tomllib.loads(path.read_text()))


def secret(name: str) -> str:
    # Credentials are never returned through the API or interpolated into commands.
    from dotenv import dotenv_values

    return (os.environ.get(name) or dotenv_values(".env").get(name) or "").strip()

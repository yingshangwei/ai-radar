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


class TranslationConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    model: str = "deepseek-v4-flash"
    review_model: str = "deepseek-v4-pro"
    revision: str = "zh-v1"
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    concurrency: int = Field(default=2, ge=1, le=4)
    max_documents: int = Field(default=100, ge=1, le=500)
    max_attempts: int = Field(default=3, ge=1, le=10)
    request_options: dict = Field(default_factory=lambda: {"thinking": {"type": "disabled"}})
    auxiliary_url: str | None = None
    auxiliary_key_env: str = "LIBRETRANSLATE_API_KEY"
    glossary: dict[str, str] = Field(default_factory=lambda: {
        "agent": "智能体；网络代理语境除外", "open-weight": "开放权重（不等于开源）",
        "formalization": "形式化；不能改成首次证明", "benchmark": "基准测试",
        "post-hoc": "事后评估", "inference": "推理", "fine-tuning": "微调",
    })


class ReadingConfig(BaseModel):
    enabled: bool = False
    max_links_per_article: int = Field(default=4, ge=1, le=10)
    max_documents: int = Field(default=24, ge=1, le=100)
    concurrency: int = Field(default=2, ge=1, le=4)
    refresh_hours: int = Field(default=24, ge=1, le=168)
    revision: str = "reading-zh-v1"
    # Explicit names only; administrators can add verified article/app destinations.
    mention_catalog: dict[str, str] = Field(default_factory=lambda: {
        "ChatGPT": "https://openai.com/chatgpt/overview/",
        "Claude Code": "https://www.anthropic.com/claude-code",
        "Gemini CLI": "https://github.com/google-gemini/gemini-cli",
        "Cursor": "https://www.cursor.com/",
    })


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
    x_page_size: int = Field(default=100, ge=10, le=100)
    x_query: str = '(AI OR "artificial intelligence" OR LLM OR agents OR robotics) -is:retweet'
    facebook_version: str = "v23.0"
    facebook_page_ids: list[str] = Field(default_factory=list)
    feeds: list[FeedConfig] = Field(default_factory=list)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    reading: ReadingConfig = Field(default_factory=ReadingConfig)

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

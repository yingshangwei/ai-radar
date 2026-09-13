import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .chat_config import ChatConfig
from .industry_config import IndustryConfig


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


def provider_model(config: ProviderConfig):
    # Explicit provider configuration always wins. The deployment may pin the
    # currently verified CLI default without invalidating saved content reviews.
    return config.model or (os.environ.get("RADAR_CODEX_DEFAULT_MODEL") if config.kind == "codex" else None)


class SummaryReviewConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    enabled: bool = True
    provider: ProviderConfig | None = None
    max_calls: int = Field(default=64, strict=True, ge=1, le=500)
    max_correction_rounds: int = Field(default=2, strict=True, ge=0, le=4)
    max_format_retries: int = Field(default=1, strict=True, ge=0, le=2)
    max_prompt_chars: int = Field(default=400_000, strict=True, ge=1000, le=2_000_000)

    @field_validator("provider")
    @classmethod
    def independent_provider(cls, value):
        if value is not None and value.kind == "extractive":
            raise ValueError("Summary fact review requires a structured model provider")
        return value


class FeedConfig(BaseModel):
    id: str
    name: str
    url: str
    authority: float = Field(default=1.0, ge=0, le=3)


class ResearchConfig(BaseModel):
    hf_enabled: bool = False
    arxiv_enabled: bool = False
    refresh_hours: int = Field(default=6, strict=True, ge=1, le=24)
    refresh_minutes: int | None = Field(default=None, strict=True, ge=5, le=1440)
    hf_limit: int = Field(default=8, strict=True, ge=1, le=30)
    hf_min_upvotes: int = Field(default=10, strict=True, ge=1, le=10000)
    arxiv_limit: int = Field(default=4, strict=True, ge=1, le=20)
    arxiv_candidates: int = Field(default=60, strict=True, ge=1, le=200)


class DiscoveryConfig(BaseModel):
    enabled: bool = False
    provider: ProviderConfig | None = None
    policy: str = "discovery-v1"
    max_age_hours: int = Field(default=48, strict=True, ge=1, le=168)
    max_candidates_per_day: int = Field(default=60, strict=True, ge=1, le=500)
    max_pending: int = Field(default=120, strict=True, ge=1, le=1000)
    max_calls_per_day: int = Field(default=20, strict=True, ge=1, le=1000)
    freshness_enabled: bool = False
    max_wait_minutes: int = Field(default=60, strict=True, ge=5, le=240)
    max_calls_per_hour: int = Field(default=40, strict=True, ge=1, le=200)
    max_tokens_per_day: int = Field(default=2_000_000, strict=True, ge=1000)
    max_calls_per_candidate: int = Field(default=2, strict=True, ge=1, le=3)
    batch_size: int = Field(default=2, strict=True, ge=1, le=4)
    session_reuse: bool = False
    session_directory: str = "./data/agent-sessions"
    session_max_turns: int = Field(default=32, strict=True, ge=1, le=100)
    session_max_age_hours: int = Field(default=168, strict=True, ge=1, le=720)
    hot_engagement: int = Field(default=100, strict=True, ge=1)
    early_score_min: int = Field(default=75, strict=True, ge=0, le=100)
    early_confidence_min: int = Field(default=75, strict=True, ge=0, le=100)
    watch_confidence_min: int = Field(default=85, strict=True, ge=0, le=100)
    auto_watch: bool = True
    max_auto_watches: int = Field(default=6, strict=True, ge=0, le=20)
    max_new_watches_per_day: int = Field(default=2, strict=True, ge=0, le=10)
    trial_days: int = Field(default=7, strict=True, ge=1, le=30)


TranslationStage = Literal["draft", "correction", "audit"]
TranslationTokenLimit = Annotated[int, Field(strict=True, ge=256, le=65536)]
TRANSLATION_RESERVED_OPTIONS = frozenset({
    "messages", "model", "response_format", "tools", "tool_choice", "functions", "function_call",
    "stream", "stream_options", "max_tokens", "max_completion_tokens", "extra_body",
})


class TranslationConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    enabled: bool = False
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    model: str = "deepseek-v4-flash"
    review_model: str = "deepseek-v4-pro"
    audit_model: str | None = None
    technical_review_provider: ProviderConfig | None = None
    review_max_rounds: int = Field(default=2, ge=1, le=4)
    revision: str = "zh-v1"
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    concurrency: int = Field(default=2, ge=1, le=4)
    max_documents: int = Field(default=100, ge=1, le=500)
    max_attempts: int = Field(default=3, ge=1, le=10)
    factual_review_only: bool = False
    individual_parts: bool = False
    request_options: dict = Field(default_factory=lambda: {"thinking": {"type": "disabled"}})
    stage_request_options: dict[TranslationStage, dict] = Field(default_factory=dict)
    max_tokens: TranslationTokenLimit = 12000
    stage_max_tokens: dict[TranslationStage, TranslationTokenLimit] = Field(default_factory=dict)
    auxiliary_url: str | None = None
    auxiliary_key_env: str = "LIBRETRANSLATE_API_KEY"
    glossary: dict[str, str] = Field(default_factory=lambda: {
        "agent": "智能体；网络代理语境除外", "open-weight": "开放权重（不等于开源）",
        "formalization": "形式化；不能改成首次证明", "benchmark": "基准测试",
        "post-hoc": "事后评估", "inference": "推理", "fine-tuning": "微调",
    })

    @field_validator("technical_review_provider")
    @classmethod
    def technical_model_provider(cls, value):
        if value is not None and value.kind == "extractive":
            raise ValueError("Technical translation review requires a structured model provider")
        return value

    @field_validator("stage_request_options", "stage_max_tokens", mode="before")
    @classmethod
    def valid_translation_stages(cls, value):
        # Reject unknown keys before Pydantic includes them in an error location.
        if isinstance(value, dict) and any(key not in {"draft", "correction", "audit"} for key in value):
            raise ValueError("Translation stage must be draft, correction or audit")
        return value

    @field_validator("request_options", "stage_request_options")
    @classmethod
    def valid_request_options(cls, value: dict, info):
        options = [value] if info.field_name == "request_options" else value.values()
        for item in options:
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Translation request option keys must be strings")
            if TRANSLATION_RESERVED_OPTIONS.intersection(item):
                raise ValueError("Translation request options contain a reserved workflow field")
        return value


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
    chat: ChatConfig = Field(default_factory=ChatConfig)
    # Nested translation validation must not echo options or configured secrets.
    model_config = ConfigDict(hide_input_in_errors=True)

    timezone: str = "Asia/Shanghai"
    daily_hour: int = Field(default=8, ge=0, le=23)
    daily_minute: int = Field(default=0, ge=0, le=59)
    collect_minutes: int = Field(default=120, ge=15)
    lookback_hours: int = Field(default=168, ge=1, le=168)
    anthropic_news_enabled: bool = True
    official_news_sources: list[Literal["seed", "deepseek", "kimi", "minimax"]] = Field(
        default_factory=list, max_length=4,
    )
    enrich_official_articles: bool = True
    min_engagement: int = Field(default=30, ge=0)
    x_max_pages: int = Field(default=2, ge=1, le=10)
    x_page_size: int = Field(default=100, ge=10, le=100)
    x_request_budget: int | None = Field(default=None, strict=True, ge=1, le=1000)
    x_discovery_requests: int | None = Field(default=None, strict=True, ge=0, le=1000)
    x_watch_freshness_first: bool = False
    x_initial_lookback_hours: int = Field(default=24, strict=True, ge=1, le=168)
    x_head_refresh_hours: int = Field(default=24, strict=True, ge=1, le=168)
    x_head_refresh_minutes: int | None = Field(default=None, strict=True, ge=1, le=1440)
    publish_priority_raw: bool = False
    x_query: str = '(AI OR "artificial intelligence" OR LLM OR agents OR robotics) -is:retweet'
    facebook_version: str = "v23.0"
    facebook_page_ids: list[str] = Field(default_factory=list)
    feeds: list[FeedConfig] = Field(default_factory=list)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    summary_review: SummaryReviewConfig = Field(default_factory=SummaryReviewConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    reading: ReadingConfig = Field(default_factory=ReadingConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    industry: IndustryConfig = Field(default_factory=IndustryConfig)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str):
        ZoneInfo(value)
        return value

    @field_validator("official_news_sources")
    @classmethod
    def unique_news_sources(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Announcement sources must be unique")
        return value


class Settings(BaseSettings):
    market_service_url: str = ""
    market_reader_token: str = Field(default="", repr=False)
    market_sync_token: str = Field(default="", repr=False)

    model_config = SettingsConfigDict(env_prefix="RADAR_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/radar.db"
    usage_database_path: str = ""
    accounts_config_path: str = ""
    accounts_database_path: str = ""
    config_path: str = "config.toml"
    reader_token: str = ""
    admin_token: str = ""
    scheduler_enabled: bool = False
    browser_worker_url: str = "http://127.0.0.1:18475"
    browser_worker_token: str = ""
    browser_public_origin: str = ""
    browser_assets_path: str = "/opt/ai-radar/browser-assets"
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

"""X data vendors are independent of the model/agent providers."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class XDataConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid")

    provider: Literal["official", "twitterapi_io"] = "official"
    api_key_env: str = "TWITTERAPI_IO_KEY"
    credentials_file: str = ""
    monthly_usd: float = Field(default=8, ge=0, le=1000, allow_inf_nan=False)
    daily_usd: float = Field(default=0.4, ge=0, le=100, allow_inf_nan=False)
    # Public price verified 2026-09-17; estimates, not a vendor invoice.
    tweet_price_usd: float = Field(default=0.00015, gt=0, le=1, allow_inf_nan=False)
    request_interval_seconds: float = Field(default=5.1, ge=0.05, le=30)
    overlap_minutes: int = Field(default=5, strict=True, ge=0, le=30)
    discovery_interval_minutes: int = Field(default=360, strict=True, ge=30, le=1440)
    shadow_enabled: bool = False
    shadow_monthly_usd: float = Field(default=2, ge=0, le=100, allow_inf_nan=False)
    shadow_daily_usd: float = Field(default=0.1, ge=0, le=10, allow_inf_nan=False)
    shadow_api_key_env: str = "APIFY_API_TOKEN"


def state_key(config, key):
    return key if config.x_data.provider == "official" else f"{config.x_data.provider}:{key}"


def vendor_secret(config, name):
    from pathlib import Path

    from dotenv import dotenv_values

    from .config import secret

    value = secret(name)
    path = config.x_data.credentials_file
    if not value and path and Path(path).is_file():
        value = (dotenv_values(path).get(name) or "").strip()
    return value

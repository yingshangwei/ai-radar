from pydantic import BaseModel, Field


class ChatConfig(BaseModel):
    enabled: bool = False
    state_directory: str = "./data/chat"
    timeout_seconds: int = Field(default=300, ge=30, le=3600)
    max_pending: int = Field(default=8, ge=1, le=30)
    queue_minutes: int = Field(default=15, ge=1, le=60)
    max_turns_per_day: int = Field(default=100, ge=1, le=500)

    agent_enabled: bool = False
    external_worker: bool = False
    workspace: str = "/opt/ai-radar/current"

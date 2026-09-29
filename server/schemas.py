"""Validated request and response schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

AuthorizationStatus = Literal["pending", "authorized", "revoked"]


class InventoryPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str = Field(min_length=36, max_length=36)
    agent_version: str | None = Field(default=None, max_length=50)
    system: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        return str(UUID(value))


class ComputerSummary(BaseModel):
    id: int
    agent_id: str
    hostname: str
    logged_user: str | None
    os_name: str | None
    os_version: str | None
    agent_version: str | None
    last_seen: datetime
    online: bool
    authorization_status: AuthorizationStatus
    channel_connected: bool = False
    channel_connected_at: datetime | None = None
    channel_last_heartbeat: datetime | None = None


class InventoryAccepted(BaseModel):
    computer_id: int
    agent_id: str
    last_seen: datetime
    authorization_status: AuthorizationStatus
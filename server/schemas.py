"""Validated request and response schemas."""

from datetime import datetime
import math
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

AuthorizationStatus = Literal["pending", "authorized", "revoked"]


class TechnicianLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        if "\x00" in value:
            raise ValueError("Nome de usuário contém caractere inválido")
        return value


class TechnicianCreate(TechnicianLogin):
    display_name: str = Field(min_length=1, max_length=160)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value):
        if "\x00" in value:
            raise ValueError("Nome contém caractere inválido")
        return value


class InventoryPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str = Field(min_length=36, max_length=36)
    agent_version: str | None = Field(default=None, max_length=50)
    system: dict[str, Any] = Field(default_factory=dict)

    @field_validator("system")
    @classmethod
    def validate_database_strings(cls, value):
        for key, maximum in {"hostname":255,"logged_user":255,"os_name":100,"os_version":20_000}.items():
            field = value.get(key)
            if field is not None and (not isinstance(field,str) or len(field) > maximum or "\x00" in field):
                raise ValueError(f"Campo system.{key} inválido")
        return value

    @model_validator(mode="after")
    def validate_portable_json(self):
        pending = [self.system,self.agent_version,*(self.model_extra or {}).values()]
        while pending:
            value = pending.pop()
            if isinstance(value,float) and not math.isfinite(value):
                raise ValueError("Inventário deve conter apenas números JSON finitos")
            if isinstance(value,str) and "\x00" in value:
                raise ValueError("Inventário não pode conter caracteres NUL")
            if isinstance(value,dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value,(list,tuple)):
                pending.extend(value)
        return self

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        return str(UUID(value))


class ComputerSummary(BaseModel):
    id: int
    agent_id: str
    hostname: str
    display_name: str
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

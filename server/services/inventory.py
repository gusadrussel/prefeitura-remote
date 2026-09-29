"""Inventory persistence and online-status calculations."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.database.models import Computer
from server.schemas import AuthorizationStatus
from server.services.agent_connections import AgentConnection


def utc_now() -> datetime:
    return datetime.now(UTC)


def upsert_inventory(session: Session, payload: dict[str, Any]) -> Computer:
    system = payload.get("system") or {}
    agent_id = payload["agent_id"]
    computer = session.scalar(select(Computer).where(Computer.agent_id == agent_id))
    if computer is None:
        computer = Computer(
            agent_id=agent_id,
            hostname=system.get("hostname") or "desconhecido",
            authorization_status="pending",
        )
        session.add(computer)
    computer.hostname = system.get("hostname") or "desconhecido"
    computer.logged_user = system.get("logged_user")
    computer.os_name = system.get("os_name")
    computer.os_version = system.get("os_version")
    computer.agent_version = payload.get("agent_version")
    computer.last_seen = utc_now()
    computer.inventory = payload
    session.commit()
    session.refresh(computer)
    return computer


def set_authorization_status(
    session: Session, computer_id: int, status: AuthorizationStatus
) -> Computer | None:
    computer = session.get(Computer, computer_id)
    if computer is None:
        return None
    computer.authorization_status = status
    session.commit()
    session.refresh(computer)
    return computer


def is_authorized(computer: Computer) -> bool:
    return computer.authorization_status == "authorized"


def is_online(last_seen: datetime, threshold_seconds: int, now: datetime | None = None) -> bool:
    last_seen = as_utc(last_seen)
    reference = now or utc_now()
    reference = as_utc(reference)
    return reference - last_seen <= timedelta(seconds=threshold_seconds)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def computer_summary(
    computer: Computer,
    threshold_seconds: int,
    connection: AgentConnection | None = None,
) -> dict[str, Any]:
    return {
        "id": computer.id,
        "agent_id": computer.agent_id,
        "hostname": computer.hostname,
        "logged_user": computer.logged_user,
        "os_name": computer.os_name,
        "os_version": computer.os_version,
        "agent_version": computer.agent_version,
        "last_seen": as_utc(computer.last_seen),
        "online": is_online(computer.last_seen, threshold_seconds),
        "authorization_status": computer.authorization_status,
        "channel_connected": bool(connection and connection.connected),
        "channel_connected_at": connection.connected_at if connection else None,
        "channel_last_heartbeat": connection.last_heartbeat if connection else None,
    }
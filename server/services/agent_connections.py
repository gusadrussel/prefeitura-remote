"""In-process registry for currently connected, authenticated agents."""

import uuid
from asyncio import Queue, QueueFull
from dataclasses import dataclass
from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class AgentConnection:
    agent_id: str
    computer_id: int
    connection_id: str
    connected: bool
    connected_at: datetime
    last_heartbeat: datetime
    outbound: Queue[dict]


class AgentConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, AgentConnection] = {}

    def connect(self, agent_id: str, computer_id: int) -> AgentConnection:
        timestamp = utc_now()
        connection = AgentConnection(
            agent_id=agent_id,
            computer_id=computer_id,
            connection_id=str(uuid.uuid4()),
            connected=True,
            connected_at=timestamp,
            last_heartbeat=timestamp,
            outbound=Queue(maxsize=8),
        )
        self._connections[agent_id] = connection
        return connection

    def heartbeat(self, agent_id: str, connection_id: str) -> AgentConnection | None:
        connection = self._connections.get(agent_id)
        if connection is None or not connection.connected or connection.connection_id != connection_id:
            return None
        connection.last_heartbeat = utc_now()
        return connection

    def disconnect(self, agent_id: str, connection_id: str) -> AgentConnection | None:
        connection = self._connections.get(agent_id)
        if connection is None or connection.connection_id != connection_id:
            return None
        connection.connected = False
        return connection

    def get(self, agent_id: str) -> AgentConnection | None:
        return self._connections.get(agent_id)

    def send(self, agent_id: str, message: dict) -> bool:
        connection = self._connections.get(agent_id)
        if connection is None or not connection.connected:
            return False
        try:
            connection.outbound.put_nowait(message)
        except QueueFull:
            return False
        return True
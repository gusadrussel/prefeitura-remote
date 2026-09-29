"""Assemble a versioned, JSON-serializable host inventory."""

from datetime import UTC, datetime
from typing import Any

from agent import __version__
from agent.collectors.cpu import collect_cpu
from agent.collectors.disk import collect_storage
from agent.collectors.gpu import collect_gpus
from agent.collectors.network import collect_network
from agent.collectors.ram import collect_memory
from agent.collectors.system import collect_system


def collect_inventory(agent_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "agent_id": agent_id,
        "agent_version": __version__,
        "collected_at": datetime.now(UTC).isoformat(),
        "system": collect_system(),
        "cpu": collect_cpu(),
        "memory": collect_memory(),
        "storage": collect_storage(),
        "network": collect_network(),
        "gpus": collect_gpus(),
    }
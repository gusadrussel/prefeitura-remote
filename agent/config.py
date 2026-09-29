"""Environment-based agent configuration."""

import os
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    server_url: str | None
    token: str | None
    interval_seconds: int
    machine_id_file: Path


def default_machine_id_file() -> Path:
    configured_path = os.getenv("PREFEITURA_AGENT_ID_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    if platform.system() == "Windows":
        base = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData"))
        return base / "PrefeituraRemote" / "agent_id"
    base = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "prefeitura-remote" / "agent_id"


def load_config() -> AgentConfig:
    try:
        interval = int(os.getenv("PREFEITURA_AGENT_INTERVAL", "300"))
    except ValueError as exc:
        raise ValueError("PREFEITURA_AGENT_INTERVAL deve ser um inteiro") from exc
    if interval < 30:
        raise ValueError("PREFEITURA_AGENT_INTERVAL deve ser pelo menos 30 segundos")
    return AgentConfig(
        server_url=os.getenv("PREFEITURA_SERVER_URL") or None,
        token=os.getenv("PREFEITURA_AGENT_TOKEN") or None,
        interval_seconds=interval,
        machine_id_file=default_machine_id_file(),
    )
"""Agent configuration."""

import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    server_url: str | None
    token: str | None
    individual_credential: bool
    interval_seconds: int
    machine_id_file: Path


def default_config_file() -> Path:
    if platform.system() == "Windows":
        base = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData"))
        return base / "PrefeituraRemote" / "config.json"

    base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "prefeitura-remote" / "config.json"


def default_machine_id_file() -> Path:
    configured_path = os.getenv("PREFEITURA_AGENT_ID_FILE")

    if configured_path:
        return Path(configured_path).expanduser()

    if platform.system() == "Windows":
        base = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData"))
        return base / "PrefeituraRemote" / "agent_id"

    base = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "prefeitura-remote" / "agent_id"


def load_file_config() -> dict:
    path = default_config_file()

    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError("config.json deve conter um objeto JSON")

        return data

    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Configuração inválida em {path}: {exc}") from exc


def load_config() -> AgentConfig:
    file_config = load_file_config()

    server_url = (
        os.getenv("PREFEITURA_SERVER_URL")
        or file_config.get("server_url")
        or None
    )

    individual_credential = (
        os.getenv("PREFEITURA_AGENT_CREDENTIAL")
        or file_config.get("credential")
        or None
    )

    token = (
        individual_credential
        or os.getenv("PREFEITURA_AGENT_TOKEN")
        or file_config.get("token")
        or None
    )

    try:
        interval = int(
            os.getenv(
                "PREFEITURA_AGENT_INTERVAL",
                str(file_config.get("interval_seconds", 300)),
            )
        )
    except ValueError as exc:
        raise ValueError(
            "PREFEITURA_AGENT_INTERVAL deve ser um inteiro"
        ) from exc

    if interval < 30:
        raise ValueError(
            "PREFEITURA_AGENT_INTERVAL deve ser pelo menos 30 segundos"
        )

    return AgentConfig(
        server_url=server_url,
        token=token,
        individual_credential=individual_credential is not None,
        interval_seconds=interval,
        machine_id_file=default_machine_id_file(),
    )
"""CLI entry point; running without a server prints a JSON inventory."""

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import load_config
from agent.connection import RECONNECT_DELAY_SECONDS, run_websocket_session, validate_server_url
from agent.inventory import collect_inventory

LOGGER = logging.getLogger("prefeitura_remote.agent")
_validate_server_url = validate_server_url


def get_agent_id(path: Path) -> str:
    """Read or create a stable per-installation UUID."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        agent_id = path.read_text(encoding="ascii").strip()
        return str(uuid.UUID(agent_id))
    except FileNotFoundError:
        agent_id = str(uuid.uuid4())
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(agent_id, encoding="ascii")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
        return agent_id
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"Não foi possível ler o identificador do agente: {path}") from exc


def send_inventory(server_url: str, token: str, inventory: dict) -> None:
    endpoint = _validate_server_url(server_url) + "/api/v1/inventory"
    payload = json.dumps(inventory, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status not in {200, 201}:
                raise RuntimeError(f"Servidor retornou HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Falha ao enviar inventário: {exc}") from exc


def run(once: bool = False, print_json: bool = False) -> int:
    try:
        config = load_config()
        agent_id = get_agent_id(config.machine_id_file)
        if config.server_url and not config.token:
            raise ValueError("Configure PREFEITURA_AGENT_TOKEN para enviar inventário")
        if config.server_url:
            _validate_server_url(config.server_url)

        if not config.server_url:
            inventory = collect_inventory(agent_id)
            print(json.dumps(inventory, ensure_ascii=False, indent=2))
            return 0

        last_inventory_sent = 0.0

        def send_inventory_update() -> None:
            nonlocal last_inventory_sent
            inventory = collect_inventory(agent_id)
            if print_json:
                print(json.dumps(inventory, ensure_ascii=False, indent=2))
            send_inventory(config.server_url or "", config.token or "", inventory)
            last_inventory_sent = time.monotonic()
            LOGGER.info("Inventário enviado para %s", inventory["system"]["hostname"])

        send_inventory_update()
        if once:
            return 0

        def send_inventory_if_due() -> None:
            if time.monotonic() - last_inventory_sent >= config.interval_seconds:
                send_inventory_update()

        while True:
            try:
                run_websocket_session(
                    config.server_url,
                    agent_id,
                    config.token or "",
                    config.interval_seconds,
                    send_inventory_if_due,
                )
            except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                LOGGER.warning("Canal persistente desconectado: %s", exc)
                time.sleep(RECONNECT_DELAY_SECONDS)
                send_inventory_if_due()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Agente de inventário da Prefeitura")
    parser.add_argument("--once", action="store_true", help="Coleta e envia uma única vez")
    parser.add_argument("--json", action="store_true", help="Imprime o inventário JSON")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(once=args.once, print_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
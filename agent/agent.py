"""CLI entry point; running without a server prints a JSON inventory."""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import time
import urllib.error
import urllib.request
import uuid
import os
import re
import subprocess
import tempfile
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import default_config_file, load_config
from agent.connection import RECONNECT_DELAY_SECONDS, run_websocket_session, validate_server_url
from agent.inventory import collect_inventory
from agent.client_app import ClientApp
from agent.http_transport import open_agent_request

LOGGER = logging.getLogger("prefeitura_remote.agent")
DIAGNOSTICS = logging.getLogger("prefeitura_remote.diagnostics")
MAX_RECONNECT_DELAY_SECONDS = 60
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
        with open_agent_request(request, timeout=20) as response:
            if response.status not in {200, 201}:
                raise RuntimeError(f"Servidor retornou HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError) as exc:
        if isinstance(exc,urllib.error.HTTPError):
            exc.close()
        raise RuntimeError(f"Falha ao enviar inventário: {exc}") from exc


def enroll_agent(server_url: str, agent_id: str, enrollment_token: str) -> str:
    endpoint = _validate_server_url(server_url) + "/api/v1/agents/enroll"
    request = urllib.request.Request(
        endpoint,
        data=b"",
        headers={
            "X-Prefeitura-Enrollment": enrollment_token,
            "X-Prefeitura-Agent-Id": agent_id,
        },
        method="POST",
    )
    try:
        with open_agent_request(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise RuntimeError(f"Falha no enrollment: HTTP {code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Falha no enrollment: {exc}") from exc
    credential = payload.get("credential")
    if not isinstance(credential, str) or not credential:
        raise RuntimeError("Servidor não retornou uma credencial de agente")
    return credential


def _is_windows():
    return os.name == "nt"


def _secure_windows_file(path):
    flags = getattr(subprocess,"CREATE_NO_WINDOW",0)
    system = Path(os.environ.get("SystemRoot",r"C:\Windows")) / "System32"
    identity = subprocess.run([str(system / "whoami.exe"),"/user","/fo","csv","/nh"],check=True,
                              stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=flags)
    match = re.search(rb"\bS-\d-(?:\d+-)+\d+\b",identity.stdout)
    if not match:
        raise RuntimeError("Não foi possível identificar o usuário para proteger a credencial")
    sid = match.group().decode("ascii")
    # Current account must retain access after dropping elevation. Never grant
    # Users/Everyone or rely on chmod() to implement a Windows DACL.
    subprocess.run([str(system / "icacls.exe"),str(path),"/inheritance:r","/grant:r",f"*{sid}:F",
                    "*S-1-5-18:F","*S-1-5-32-544:F"],check=True,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)


def save_individual_credential(config_path: Path, server_url: str, credential: str, interval_seconds: int) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "server_url": server_url,
        "credential": credential,
        "interval_seconds": interval_seconds,
    }
    descriptor, name = tempfile.mkstemp(prefix="credential-",suffix=".tmp",dir=config_path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        if _is_windows():
            _secure_windows_file(temporary)  # empty file: no secret written until ACL succeeds
        else:
            temporary.chmod(0o600)
        with temporary.open("w",encoding="utf-8") as file:
            file.write(json.dumps(data,ensure_ascii=False,indent=4)+"\n")
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(config_path)
    except subprocess.SubprocessError:
        raise RuntimeError("Falha ao proteger credencial; configuração anterior preservada") from None
    finally:
        temporary.unlink(missing_ok=True)



def reconnect_delay(attempt: int) -> float:
    """Exponential backoff bounded at one minute."""
    attempt = max(0, int(attempt))
    return min(MAX_RECONNECT_DELAY_SECONDS, RECONNECT_DELAY_SECONDS * (2 ** min(attempt, 5)))


def _enroll_with_retry(server_url, agent_id, enrollment_token, *, once=False):
    attempt = 0
    while True:
        try:
            return enroll_agent(server_url,agent_id,enrollment_token)
        except (OSError,RuntimeError) as error:
            cause = error.__cause__
            retryable = isinstance(cause,(urllib.error.URLError,TimeoutError))
            if isinstance(cause,urllib.error.HTTPError):
                retryable = cause.code in {429,500,502,503,504}
                cause.close()
            if once or not retryable:
                raise
            LOGGER.warning("Enrollment temporariamente indisponível; nova tentativa com backoff")
            time.sleep(reconnect_delay(attempt))
            attempt += 1

def _run(once: bool = False, print_json: bool = False, config_override=None) -> int:
    try:
        config = config_override if config_override is not None else load_config()
        agent_id = getattr(config,'provisioned_agent_id',None) or get_agent_id(config.machine_id_file)
        if config.server_url:
            _validate_server_url(config.server_url)
            if not config.token:
                if not config.enrollment_token:
                    raise ValueError("Agente não configurado: credencial individual ou enrollment necessário")
                credential = _enroll_with_retry(config.server_url, agent_id, config.enrollment_token, once=once)
                save_individual_credential(
                    default_config_file(),
                    config.server_url,
                    credential,
                    config.interval_seconds,
                )
                config = load_config()
            elif not config.individual_credential:
                LOGGER.warning("Usando credencial global legada; provisionar credencial individual")

        if not config.server_url:
            inventory = collect_inventory(agent_id)
            print(json.dumps(inventory, ensure_ascii=False, indent=2))
            return 0

        last_inventory_sent = None
        pending_inventory = None
        pending_collected_at = 0.0
        reconnect_attempt = 0
        client_app = ClientApp()
        managed_session = config_override is not None and not once
        if managed_session and not client_app.start({}):
            DIAGNOSTICS.error("Interface do cliente indisponível; canal não iniciado")
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "A interface do cliente não pôde ser aberta. O canal de suporte não foi iniciado.",
                "Prefeitura Remote", 0x10,
            )
            return 1

        def send_inventory_update() -> None:
            nonlocal last_inventory_sent, pending_inventory, pending_collected_at
            if pending_inventory is None or time.monotonic()-pending_collected_at >= config.interval_seconds:
                pending_inventory = collect_inventory(agent_id)
                pending_collected_at = time.monotonic()
                if not managed_session:
                    client_app.start(pending_inventory)
                if print_json:
                    print(json.dumps(pending_inventory, ensure_ascii=False, indent=2))
            inventory = pending_inventory
            client_app.update(inventory, "")
            send_inventory(config.server_url or "", config.token or "", inventory)
            last_inventory_sent = time.monotonic()
            pending_inventory = None
            LOGGER.info("Inventário enviado")

        if once:
            try:
                send_inventory_update()
                return 0
            finally:
                client_app.stop()

        def send_inventory_if_due() -> None:
            if last_inventory_sent is None or time.monotonic() - last_inventory_sent >= config.interval_seconds:
                send_inventory_update()

        try:
            while not (managed_session and client_app.closed.is_set()):
                try:
                    send_inventory_if_due()
                    run_websocket_session(
                        config.server_url, agent_id, config.token or "", config.interval_seconds,
                        send_inventory_if_due, individual_credential=config.individual_credential,
                        client_app=client_app, stop_event=client_app.closed if managed_session else None,
                    )
                    reconnect_attempt = 0
                except (OSError, RuntimeError, TimeoutError, ValueError) as error:
                    LOGGER.warning("Conexão indisponível; tentativa será repetida (%s)",type(error).__name__)
                    DIAGNOSTICS.warning("Reconexão após falha tipo=%s", type(error).__name__)
                client_app.status("Canal desconectado — tentando reconectar")
                delay = reconnect_delay(reconnect_attempt)
                if managed_session:
                    client_app.closed.wait(delay)
                else:
                    time.sleep(delay)
                reconnect_attempt += 1
            return 0
        finally:
            client_app.stop()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1


def run(once=False, print_json=False, managed=False):
    if not managed:
        return _run(once,print_json)
    from agent.managed import ManagedContextDenied, runtime_profile
    try:
        with runtime_profile() as config:
            handler = None
            try:
                local_data = os.environ.get("LOCALAPPDATA")
                if local_data and not once:
                    directory = Path(local_data) / "PrefeituraRemote"
                    directory.mkdir(parents=True, exist_ok=True)
                    handler = RotatingFileHandler(directory / "conexao.log", maxBytes=128 * 1024,
                                                  backupCount=1, encoding="utf-8")
                    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                    DIAGNOSTICS.addHandler(handler)
            except OSError:
                LOGGER.warning("Diagnóstico local indisponível")
            try:
                return _run(once,print_json,config_override=config)
            finally:
                if handler is not None:
                    DIAGNOSTICS.removeHandler(handler)
                    handler.close()
    except ManagedContextDenied:
        # HKLM Run also invokes other users: no inventory/UI/network in that context.
        return 0
    except (OSError,RuntimeError,ValueError):
        LOGGER.error('Falha ao validar o perfil gerenciado; agente não iniciado')
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Agente de inventário da Prefeitura")
    parser.add_argument("--once", action="store_true", help="Coleta e envia uma única vez")
    parser.add_argument("--json", action="store_true", help="Imprime o inventário JSON")
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--managed',action='store_true')
    modes.add_argument('--provision-managed',metavar='SOURCE')
    modes.add_argument('--activate-managed',metavar='SERVER_URL')
    modes.add_argument('--verify-managed-install',action='store_true')
    modes.add_argument('--deactivate-managed-install',action='store_true')
    modes.add_argument('--write-provision-source',metavar='DESTINATION')
    modes.add_argument('--distribution-self-check',action='store_true')
    parser.add_argument('--replace-managed-profile',action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.replace_managed_profile and not args.provision_managed:
        parser.error('--replace-managed-profile exige --provision-managed')
    if args.activate_managed:
        from agent.activation import run_activation
        return run_activation(args.activate_managed)
    if args.distribution_self_check:
        from agent.windows_security import WindowsSecurity
        WindowsSecurity()  # resolves API signatures, never calls profile/token/ACL operations
        print(json.dumps({'managed_schema':'prefeitura-managed-v1','native_bindings':'loaded',
                          'desktop_accessed':False,'network_started':False,'acl_exercised':False}))
        return 0
    if args.provision_managed or args.verify_managed_install or args.deactivate_managed_install or args.write_provision_source:
        from agent.managed import provision, verify_install, deactivate, write_provision_source
        from agent.windows_security import WindowsSecurity
        try:
            security=WindowsSecurity()
            if not security.context()['elevated'] or (not args.write_provision_source and security.running()):
                raise PermissionError('Operação administrativa indisponível')
            if args.write_provision_source:
                raw=sys.stdin.buffer.read(65537)
                if len(raw)>65536: raise ValueError('Origem excede o limite')
                write_provision_source(args.write_provision_source,raw,security)
            elif args.provision_managed:
                provision(args.provision_managed,replace=args.replace_managed_profile,security=security)
            elif args.deactivate_managed_install:
                deactivate(security)
            else:
                verify_install(security)
            print('Perfil gerenciado validado')
            return 0
        except (OSError,RuntimeError,ValueError):
            LOGGER.error('Provisionamento/verificação falhou; consulte o roteiro administrativo')
            return 1
    return run(once=args.once, print_json=args.json, managed=args.managed)


if __name__ == "__main__":
    raise SystemExit(main())

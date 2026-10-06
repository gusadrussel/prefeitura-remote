"""Authenticated persistent agent channel using a deliberately small protocol."""

import asyncio
import hashlib
import hmac
import ipaddress
import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.database.models import Computer
from server.services.remote_control import (
    valid_control_start_ack,
    valid_control_stop_ack,
    valid_screen_start_ack,
    valid_screen_stop_ack,
)
from server.services.agent_credentials import has_active_agent_credential
from server.services.rate_limits import peer_address
from server.services.agent_credentials import verify_agent_credential

LOGGER = logging.getLogger("prefeitura_remote.websocket")
websocket_router = APIRouter()
CHANNEL_CREDENTIAL_CONTEXT = b"prefeitura-remote:agent-channel:v1:"


def derive_agent_channel_credential(agent_id: str, agent_secret: str) -> str:
    message = CHANNEL_CREDENTIAL_CONTEXT + agent_id.encode("utf-8")
    return hmac.new(agent_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def hello_ack_message(agent_id: str, connected_at: datetime, heartbeat_interval: float) -> dict:
    return {
        "type": "hello_ack",
        "agent_id": agent_id,
        "connected_at": connected_at.isoformat(),
        "heartbeat_interval": heartbeat_interval,
    }


def heartbeat_ack_message() -> dict:
    return {"type": "heartbeat_ack", "timestamp": datetime.now(UTC).isoformat()}


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_secure_or_loopback(websocket: WebSocket) -> bool:
    scheme = str(websocket.scope.get("scheme", "")).lower()
    client = websocket.client
    return scheme == "wss" or (
        scheme == "ws"
        and _is_loopback_host(websocket.url.hostname)
        and client is not None
        and _is_loopback_host(client.host)
    )


def _credential_is_valid(websocket: WebSocket, agent_id: str) -> bool:
    settings = websocket.app.state.settings
    authorization = websocket.headers.get("authorization", "")
    scheme, _, credential = authorization.partition(" ")
    provided = credential.strip() if scheme.lower() == "bearer" else ""
    session: Session = websocket.app.state.session_factory()
    try:
        computer = session.scalar(select(Computer).where(Computer.agent_id == agent_id))
        has_individual_credential = bool(
            computer
            and (computer.agent_credential_hash is not None or computer.agent_credential_revoked)
        )
        if computer and computer.agent_credential_hash is not None:
            return (
                not computer.agent_credential_revoked
                and verify_agent_credential(provided, computer.agent_credential_hash)
            )
        if has_individual_credential or not settings.allow_legacy_agent_token:
            return False
        legacy_secret = settings.agent_token
        if not legacy_secret:
            return False
        expected = derive_agent_channel_credential(agent_id, legacy_secret)
        return bool(provided and hmac.compare_digest(provided, expected))
    finally:
        session.close()


def _websocket_rate_key(websocket: WebSocket, kind: str, resource_id: str) -> str:
    return f"websocket:{kind}:{peer_address(websocket.client)}:{resource_id}"


def _websocket_connection_allowed(websocket: WebSocket, kind: str, resource_id: str) -> bool:
    settings = websocket.app.state.settings
    return websocket.app.state.rate_limiter.allow(
        _websocket_rate_key(websocket, kind, resource_id),
        limit=settings.websocket_connections_per_window,
        window_seconds=settings.rate_limit_window_seconds,
    )

import base64
import binascii

def _find_authorized_computer(websocket: WebSocket, agent_id: str) -> Computer | None:
    session: Session = websocket.app.state.session_factory()
    try:
        computer = session.scalar(select(Computer).where(Computer.agent_id == agent_id))
        if (
            computer is None
            or computer.authorization_status != "authorized"
            or not has_active_agent_credential(computer, websocket.app.state.settings)
        ):
            return None
        return computer
    finally:
        session.close()


def _valid_screen_frame(message: dict, settings) -> bool:
    data = message.get("data")
    width = message.get("width")
    height = message.get("height")
    sequence = message.get("sequence")
    if (
        set(message) != {"type", "format", "data", "width", "height", "sequence"}
        or message.get("type") != "screen.frame"
        or message.get("format") != "jpeg"
        or not isinstance(data, str)
        or not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
        or width < 1
        or height < 1
        or width > min(settings.screen_max_width, 1280)
        or height > min(settings.screen_max_height, 720)
        or len(data) > (settings.screen_max_frame_bytes + 2) // 3 * 4
    ):
        return False
    try:
        frame = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        return False
    return (
        len(frame) <= settings.screen_max_frame_bytes
        and frame.startswith(b"\xff\xd8")
        and frame.endswith(b"\xff\xd9")
    )


async def _close_policy(websocket: WebSocket, reason: str) -> None:
    await websocket.close(code=1008, reason=reason)


@websocket_router.websocket("/ws/agents/{agent_id}")
async def agent_channel(websocket: WebSocket, agent_id: str) -> None:
    if not _is_secure_or_loopback(websocket):
        await _close_policy(websocket, "WSS obrigatório fora de localhost")
        return
    settings = websocket.app.state.settings
    peer = peer_address(websocket.client)
    if not _websocket_connection_allowed(websocket, "agent", agent_id):
        await websocket.close(code=1013, reason="Limite de conexões atingido")
        return
    auth_key = f"websocket-auth-failure:{peer}"
    if not _credential_is_valid(websocket, agent_id):
        if not websocket.app.state.rate_limiter.allow(
            auth_key,
            limit=settings.auth_failures_per_window,
            window_seconds=settings.rate_limit_window_seconds,
        ):
            await websocket.close(code=1013, reason="Limite de autenticação atingido")
            return
        await _close_policy(websocket, "Credencial do agente inválida")
        return
    websocket.app.state.rate_limiter.reset(auth_key)
    websocket.app.state.rate_limiter.reset(auth_key)
    computer = _find_authorized_computer(websocket, agent_id)
    if computer is None:
        await _close_policy(websocket, "Agente inexistente ou não autorizado")
        return

    await websocket.accept()
    registry = websocket.app.state.agent_connections
    screen_streams = websocket.app.state.screen_streams
    connection = None
    receive_task = None
    command_task = None
    try:
        try:
            hello = await asyncio.wait_for(
                websocket.receive_json(), timeout=settings.agent_hello_timeout_seconds
            )
        except TimeoutError:
            await _close_policy(websocket, "Mensagem hello não recebida")
            return
        if (
            not isinstance(hello, dict)
            or hello.get("type") != "hello"
            or hello.get("agent_id") != agent_id
        ):
            await _close_policy(websocket, "Handshake hello inválido")
            return

        # Recheck after hello so a concurrent revocation cannot create an active session.
        computer = _find_authorized_computer(websocket, agent_id)
        if computer is None:
            await _close_policy(websocket, "Agente não autorizado")
            return
        existing = registry.get(agent_id)
        if existing and existing.connected:
            await _close_policy(websocket, "Já existe uma sessão ativa para este agente")
            return

        connection = registry.connect(agent_id, computer.id)
        await websocket.send_json(
            hello_ack_message(
                agent_id,
                connection.connected_at,
                settings.agent_heartbeat_interval_seconds,
            )
        )
        loop = asyncio.get_running_loop()
        last_heartbeat = loop.time()
        receive_task = asyncio.create_task(websocket.receive_json())
        command_task = asyncio.create_task(connection.outbound.get())
        while True:
            remaining = settings.agent_heartbeat_timeout_seconds - (loop.time() - last_heartbeat)
            if remaining <= 0:
                await websocket.close(code=1001, reason="Heartbeat timeout")
                break
            done, _ = await asyncio.wait(
                {receive_task, command_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await websocket.close(code=1001, reason="Heartbeat timeout")
                break

            if command_task in done:
                command = command_task.result()
                await websocket.send_json(command)
                command_task = asyncio.create_task(connection.outbound.get())

            if receive_task not in done:
                continue
            message = receive_task.result()
            receive_task = asyncio.create_task(websocket.receive_json())
            if not isinstance(message, dict):
                await websocket.close(code=1003, reason="Mensagem JSON inválida")
                break

            message_type = message.get("type")
            if message_type == "heartbeat":
                if not _credential_is_valid(websocket, agent_id):
                    await _close_policy(websocket, "Credencial do agente revogada ou inválida")
                    break
                if _find_authorized_computer(websocket, agent_id) is None:
                    await _close_policy(websocket, "Autorização revogada")
                    break
                connection = registry.heartbeat(agent_id, connection.connection_id)
                if connection is None:
                    await _close_policy(websocket, "Sessão substituída")
                    break
                last_heartbeat = loop.time()
                await websocket.send_json(heartbeat_ack_message())
            elif message_type == "disconnect":
                await websocket.send_json({"type": "disconnect_ack"})
                break
            elif message_type == "screen.start_ack":
                if not valid_screen_start_ack(message, settings):
                    await websocket.close(code=1003, reason="screen.start_ack inválido")
                    break
                if not screen_streams.agent_started(agent_id, message) and message["accepted"]:
                    registry.send(agent_id, {"type": "screen.stop"})
            elif message_type == "screen.frame":
                if not _valid_screen_frame(message, settings):
                    await websocket.close(code=1003, reason="screen.frame inválido")
                    break
                if (
                    not _credential_is_valid(websocket, agent_id)
                    or _find_authorized_computer(websocket, agent_id) is None
                ):
                    registry.send(agent_id, {"type": "screen.stop"})
                    screen_streams.agent_disconnected(agent_id)
                    await _close_policy(websocket, "Autorização revogada")
                    break
                viewer = screen_streams.get(agent_id)
                if viewer is not None and viewer.state == "streaming":
                    screen_streams.publish_frame(agent_id, message)
                elif viewer is None or viewer.state == "idle":
                    registry.send(agent_id, {"type": "screen.stop"})
                    await _close_policy(websocket, "Frame sem screen.start ativo")
                    break
            elif message_type == "screen.stop_ack":
                if not valid_screen_stop_ack(message):
                    await websocket.close(code=1003, reason="screen.stop_ack inválido")
                    break
                screen_streams.agent_stopped(agent_id, message)
            elif message_type == "control.start_ack":
                if not valid_control_start_ack(message):
                    LOGGER.warning("remote_control_ack_rejected reason=invalid_message agent_id=%s", agent_id)
                    await websocket.close(code=1003, reason="control.start_ack inválido")
                    break
                LOGGER.info(
                    "remote_control_start_ack accepted=%s reason=%s agent_id=%s",
                    message["accepted"],
                    message.get("reason", "accepted"),
                    agent_id,
                )
                if not screen_streams.agent_control_started(agent_id, message) and message["accepted"]:
                    registry.send(agent_id, {"type": "control.stop"})
            elif message_type == "control.stop_ack":
                if not valid_control_stop_ack(message):
                    LOGGER.warning("remote_control_stop_ack_rejected reason=invalid_message agent_id=%s", agent_id)
                    await websocket.close(code=1003, reason="control.stop_ack inválido")
                    break
                LOGGER.info(
                    "remote_control_stop_ack reason=%s agent_id=%s",
                    message.get("reason", "operator_request"),
                    agent_id,
                )
                screen_streams.agent_control_stopped(agent_id, message)
            else:
                await websocket.close(code=1003, reason="Tipo de mensagem não suportado")
                break
    except asyncio.CancelledError:
        LOGGER.debug("Canal do agente cancelado: %s", agent_id)
    except WebSocketDisconnect:
        pass
    except (RuntimeError, ValueError):
        LOGGER.exception("Erro no canal do agente %s", agent_id)
    finally:
        for task in (receive_task, command_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (receive_task, command_task) if task is not None),
            return_exceptions=True,
        )
        screen_streams.agent_disconnected(agent_id)
        if connection is not None:
            registry.disconnect(agent_id, connection.connection_id)
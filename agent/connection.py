"""Persistent WebSocket client for the agent heartbeat protocol."""

import base64
import json
import hashlib
import hmac
import ipaddress
import time
from collections.abc import Callable
from threading import Event
from urllib.parse import quote, urlsplit, urlunsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from agent.screen_capture import ScreenCapture

HEARTBEAT_ACK_TIMEOUT_SECONDS = 30
CLOSE_ACK_TIMEOUT_SECONDS = 2
RECONNECT_DELAY_SECONDS = 5
CHANNEL_CREDENTIAL_CONTEXT = b"prefeitura-remote:agent-channel:v1:"


def validate_server_url(server_url: str) -> str:
    parsed = urlsplit(server_url)
    if parsed.scheme == "https" and parsed.hostname:
        return server_url.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname:
        if parsed.hostname.lower() == "localhost":
            return server_url.rstrip("/")
        try:
            if ipaddress.ip_address(parsed.hostname).is_loopback:
                return server_url.rstrip("/")
        except ValueError:
            pass
    raise ValueError("O servidor deve usar HTTPS; HTTP só é permitido em loopback local")


def websocket_url(server_url: str, agent_id: str) -> str:
    validated_url = validate_server_url(server_url)
    parsed = urlsplit(validated_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path.rstrip('/')}/ws/agents/{quote(agent_id, safe='')}"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def derive_channel_credential(agent_id: str, agent_secret: str) -> str:
    message = CHANNEL_CREDENTIAL_CONTEXT + agent_id.encode("utf-8")
    return hmac.new(agent_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _send_json(connection, message: dict) -> None:
    connection.send(json.dumps(message, separators=(",", ":")))


def _receive_json(connection, timeout: float) -> dict:
    message = json.loads(connection.recv(timeout=timeout))
    if not isinstance(message, dict):
        raise RuntimeError("Mensagem inválida recebida no canal do agente")
    return message


def validate_hello_ack(message: dict, agent_id: str) -> None:
    if message.get("type") != "hello_ack" or message.get("agent_id") != agent_id:
        raise RuntimeError("Resposta hello_ack inválida")


def validate_heartbeat_ack(message: dict) -> None:
    if message.get("type") != "heartbeat_ack":
        raise RuntimeError("Resposta heartbeat_ack inválida")


def _close_capture(capture: ScreenCapture) -> None:
    try:
        capture.close()
    except Exception:
        pass


def run_websocket_session(
    server_url: str,
    agent_id: str,
    token: str,
    inventory_interval_seconds: int,
    inventory_callback: Callable[[], None],
    stop_event: Event | None = None,
    screen_capture_factory: Callable[..., ScreenCapture] | None = None,
) -> None:
    if not token:
        raise ValueError("Configure PREFEITURA_AGENT_TOKEN para autenticar o canal")
    target = websocket_url(server_url, agent_id)
    capture = None
    try:
        with connect(
            target,
            additional_headers={
                "Authorization": f"Bearer {derive_channel_credential(agent_id, token)}"
            },
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=64 * 1024,
        ) as connection:
            _send_json(
                connection,
                {"type": "hello", "agent_id": agent_id, "version": "0.1.0"},
            )
            hello_ack = _receive_json(connection, timeout=10)
            validate_hello_ack(hello_ack, agent_id)

            heartbeat_interval = max(1.0, float(hello_ack.get("heartbeat_interval", 30)))
            last_inventory_at = time.monotonic()
            next_heartbeat_at = time.monotonic()
            next_frame_at: float | None = None
            pending_frame: tuple[bytes, int, int] | None = None
            frame_sequence = 0
            frame_rate = 1

            while True:
                if stop_event is not None and stop_event.is_set():
                    break

                now = time.monotonic()
                if capture is not None and next_frame_at is not None and now >= next_frame_at:
                    try:
                        if pending_frame is None:
                            frame, width, height = capture.capture_jpeg()
                        else:
                            frame, width, height = pending_frame
                            pending_frame = None
                        _send_json(
                            connection,
                            {
                                "type": "screen.frame",
                                "format": "jpeg",
                                "data": base64.b64encode(frame).decode("ascii"),
                                "width": width,
                                "height": height,
                                "sequence": frame_sequence,
                            },
                        )
                        frame_sequence += 1
                        next_frame_at = time.monotonic() + 1 / frame_rate
                    except Exception:
                        _close_capture(capture)
                        capture = None
                        pending_frame = None
                        next_frame_at = None
                        _send_json(
                            connection,
                            {
                                "type": "screen.stop_ack",
                                "stopped": True,
                                "reason": "capture_failed",
                            },
                        )

                now = time.monotonic()
                if now >= next_heartbeat_at:
                    _send_json(connection, {"type": "heartbeat"})
                    next_heartbeat_at = now + heartbeat_interval

                deadlines = [next_heartbeat_at]
                if capture is not None and next_frame_at is not None:
                    deadlines.append(next_frame_at)
                timeout = max(0.01, min(deadlines) - time.monotonic())
                if stop_event is not None:
                    timeout = min(timeout, 0.25)
                try:
                    message = _receive_json(connection, timeout=timeout)
                except TimeoutError:
                    continue

                message_type = message.get("type")
                if message_type == "heartbeat_ack":
                    validate_heartbeat_ack(message)
                    now = time.monotonic()
                    if now - last_inventory_at >= inventory_interval_seconds:
                        inventory_callback()
                        last_inventory_at = now
                elif message_type == "screen.start":
                    if capture is not None:
                        _send_json(
                            connection,
                            {"type": "screen.start_ack", "accepted": False, "reason": "already_running"},
                        )
                        continue
                    candidate = None
                    try:
                        factory = screen_capture_factory or ScreenCapture
                        frame_rate = max(1, min(int(message.get("max_fps", 3)), 5))
                        candidate = factory(
                            max_width=int(message.get("max_width", 1280)),
                            max_height=int(message.get("max_height", 720)),
                            quality=int(message.get("jpeg_quality", 55)),
                            max_bytes=int(message.get("max_frame_bytes", 750_000)),
                        )
                        pending_frame = candidate.capture_jpeg()
                        capture = candidate
                        next_frame_at = time.monotonic()
                        _send_json(
                            connection,
                            {
                                "type": "screen.start_ack",
                                "accepted": True,
                                "width": pending_frame[1],
                                "height": pending_frame[2],
                                "max_fps": frame_rate,
                            },
                        )
                    except Exception:
                        if candidate is not None:
                            _close_capture(candidate)
                        capture = None
                        pending_frame = None
                        next_frame_at = None
                        _send_json(
                            connection,
                            {"type": "screen.start_ack", "accepted": False, "reason": "capture_unavailable"},
                        )
                elif message_type == "screen.stop":
                    if capture is not None:
                        _close_capture(capture)
                        capture = None
                    pending_frame = None
                    next_frame_at = None
                    _send_json(connection, {"type": "screen.stop_ack", "stopped": True})
                else:
                    raise RuntimeError("Mensagem não suportada recebida do servidor")

            if capture is not None:
                _close_capture(capture)
                capture = None
            _send_json(connection, {"type": "disconnect"})
            try:
                _receive_json(connection, timeout=CLOSE_ACK_TIMEOUT_SECONDS)
            except TimeoutError:
                pass
    except (OSError, TimeoutError, WebSocketException) as exc:
        raise RuntimeError(f"Canal WebSocket indisponível: {exc}") from exc
    finally:
        if capture is not None:
            _close_capture(capture)
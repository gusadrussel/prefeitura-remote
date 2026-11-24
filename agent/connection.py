"""Persistent WebSocket client for the agent heartbeat protocol."""

import base64
import json
import hashlib
import hmac
import ipaddress
import logging
import time
from collections.abc import Callable
from threading import Event
from urllib.parse import quote, urlsplit, urlunsplit

from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

from agent import __version__
from agent.remote_input import RemoteInputController, valid_control_input
from agent.consent import ConsentController
from agent.screen_capture import ScreenCapture
from agent.client_app import ClientApp

HEARTBEAT_ACK_TIMEOUT_SECONDS = 30
CLOSE_ACK_TIMEOUT_SECONDS = 2
RECONNECT_DELAY_SECONDS = 5
CHANNEL_CREDENTIAL_CONTEXT = b"prefeitura-remote:agent-channel:v1:"
LOGGER = logging.getLogger("prefeitura_remote.agent.control")
DIAGNOSTICS = logging.getLogger("prefeitura_remote.diagnostics")


def validate_server_url(server_url: str) -> str:
    if not isinstance(server_url,str) or len(server_url)>2048 or any(ord(c)<32 or ord(c)==127 for c in server_url):
        raise ValueError('URL do servidor inválida')
    parsed = urlsplit(server_url)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError('URL do servidor não pode conter credenciais, query ou fragmento')
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


def _valid_agent_command(message: dict) -> bool:
    message_type = message.get("type")
    if not isinstance(message_type, str):
        return False
    if message_type == "heartbeat_ack":
        return set(message).issubset({"type", "timestamp"}) and (
            "timestamp" not in message or isinstance(message["timestamp"], str)
        )
    if message_type == "screen.start":
        allowed = {
            "type", "max_fps", "max_width", "max_height", "jpeg_quality", "max_frame_bytes"
        }
        if not set(message).issubset(allowed):
            return False
        bounds = {
            "max_fps": (1, 5),
            "max_width": (1, 1280),
            "max_height": (1, 720),
            "jpeg_quality": (30, 85),
            "max_frame_bytes": (1, 750_000),
        }
        return all(
            isinstance(message[key], int)
            and not isinstance(message[key], bool)
            and minimum <= message[key] <= maximum
            for key, (minimum, maximum) in bounds.items()
            if key in message
        )
    if message_type in {"screen.stop", "control.start", "control.stop"}:
        return set(message) == {"type"}
    if message_type == "control.input":
        return valid_control_input(message)
    if message_type == "session.stop":
        return (
            set(message) == {"type", "reason"}
            and isinstance(message["reason"], str)
            and message["reason"] in {"authorization_revoked", "channel_unavailable"}
        )
    if message_type == "session.consent_request":
        return (
            set(message) == {
                "type", "request_id", "technician_name", "mode", "hostname", "expires_in"
            }
            and isinstance(message["request_id"], str)
            and isinstance(message["technician_name"], str)
            and message["mode"] in {"view", "control"}
            and isinstance(message["hostname"], str)
            and isinstance(message["expires_in"], int)
            and not isinstance(message["expires_in"], bool)
            and 1 <= message["expires_in"] <= 300
        )
    if message_type == "session.consent_result":
        return (
            set(message) == {"type", "request_id", "approved", "reason"}
            and isinstance(message["request_id"], str)
            and isinstance(message["approved"], bool)
            and isinstance(message["reason"], str)
        )
    if message_type == "session.remote_stop":
        return set(message) == {"type", "reason"} and isinstance(message["reason"], str)
    if message_type == "chat.message":
        return (set(message) == {"type", "message_id", "sender", "message"}
                and isinstance(message["message_id"], int) and isinstance(message["sender"], str)
                and isinstance(message["message"], str) and 1 <= len(message["message"]) <= 2000)
    return False


def run_websocket_session(
    server_url: str,
    agent_id: str,
    token: str,
    inventory_interval_seconds: int,
    inventory_callback: Callable[[], None],
    stop_event: Event | None = None,
    screen_capture_factory: Callable[..., ScreenCapture] | None = None,
    remote_input_factory: Callable[[], RemoteInputController] | None = None,
    individual_credential: bool = False,
    client_app: ClientApp | None = None,
) -> None:
    if not token:
        raise ValueError("Configure PREFEITURA_AGENT_TOKEN para autenticar o canal")
    target = websocket_url(server_url, agent_id)
    capture = None
    remote_input = None
    consent = ConsentController()
    control_consent = ConsentController()
    screen_size: tuple[int, int] | None = None
    try:
        with connect(
            target,
            additional_headers={
                "Authorization": f"Bearer {token if individual_credential else derive_channel_credential(agent_id, token)}"
            },
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=64 * 1024,
        ) as connection:
            _send_json(connection, {"type": "hello", "agent_id": agent_id, "version": __version__})
            hello_ack = _receive_json(connection, timeout=10)
            validate_hello_ack(hello_ack, agent_id)
            DIAGNOSTICS.info("Canal autenticado")
            if client_app is not None:
                client_app.status("Conectado ao canal de suporte")

            heartbeat_interval = max(1.0, float(hello_ack.get("heartbeat_interval", 30)))
            last_inventory_at = time.monotonic()
            next_heartbeat_at = time.monotonic()
            next_frame_at: float | None = None
            pending_frame: tuple[bytes, int, int] | None = None
            frame_sequence = 0
            frame_rate = 1
            last_sharing = (False, False)
            last_pending = False
            if client_app is not None:
                client_app.end_session.clear()

            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if client_app is not None and client_app.end_session.is_set():
                    client_app.end_session.clear()
                    consent.request_stop()
                consent_event = consent.poll()
                if consent_event is None:
                    consent_event = control_consent.poll()
                stopping_locally = consent_event is not None and consent_event.kind == "user_stop"
                if capture is not None and not consent.is_active("view") and not stopping_locally:
                    if remote_input is not None:
                        remote_input.stop()
                        remote_input = None
                    _close_capture(capture)
                    capture = None
                    screen_size = pending_frame = next_frame_at = None
                    control_consent.stop()
                    _send_json(connection, {"type":"screen.stop_ack", "stopped":True, "reason":"session_inactive"})
                if remote_input is not None and not control_consent.is_active("control") and not stopping_locally:
                    remote_input.stop()
                    remote_input = None
                    _send_json(connection, {"type":"control.stop_ack", "stopped":True, "reason":"session_inactive"})

                if remote_input is not None and getattr(remote_input, "failed", False):
                    control_consent.stop()
                    failure_reason = getattr(remote_input, "failure_reason", None) or "backend_error"
                    remote_input.stop()
                    remote_input = None
                    LOGGER.error("remote_control_stopped reason=input_failed detail=%s", failure_reason)
                    _send_json(
                        connection,
                        {
                            "type": "control.stop_ack",
                            "stopped": True,
                            "reason": "input_failed",
                        },
                    )

                if consent_event is not None:
                    if consent_event.kind in {"accept", "reject"} and consent_event.request_id:
                        _send_json(
                            connection,
                            {
                                "type": "session.consent_response",
                                "request_id": consent_event.request_id,
                                "accepted": consent_event.kind == "accept",
                            },
                        )
                    elif consent_event.kind == "user_stop" and consent_event.request_id:
                        # Stop locally before attempting any network notification.
                        consent.stop()
                        control_consent.stop()
                        if remote_input is not None:
                            remote_input.stop()
                            remote_input = None
                        if capture is not None:
                            _close_capture(capture)
                            capture = None
                        screen_size = pending_frame = next_frame_at = None
                        _send_json(
                            connection,
                            {
                                "type": "session.user_stop",
                                "request_id": consent_event.request_id,
                            },
                        )

                if client_app is not None:
                    outgoing_chat = client_app.next_chat()
                    if outgoing_chat is not None:
                        _send_json(connection, {"type": "chat.message", "message_id": 0,
                                                "sender": "client", "message": outgoing_chat})

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
                        consent.stop()
                        control_consent.stop()
                        if remote_input is not None:
                            LOGGER.error("remote_control_stopped reason=capture_failed")
                            remote_input.stop()
                            remote_input = None
                            _send_json(
                                connection,
                                {
                                    "type": "control.stop_ack",
                                    "stopped": True,
                                    "reason": "capture_failed",
                                },
                            )
                        _close_capture(capture)
                        capture = None
                        screen_size = None
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

                sharing = (capture is not None, remote_input is not None)
                if client_app is not None and sharing != last_sharing:
                    client_app.session_status("Controle remoto ativo" if sharing[1] else
                                              "Visualização remota ativa" if sharing[0] else
                                              "Compartilhamento inativo", active=sharing[0] or sharing[1])
                last_sharing = sharing
                if client_app is not None:
                    pending = bool(consent.is_pending() or control_consent.is_pending())
                    if pending != last_pending:
                        client_app.request_status(pending)
                    last_pending = pending

                deadlines = [next_heartbeat_at]
                if capture is not None and next_frame_at is not None:
                    deadlines.append(next_frame_at)
                timeout = max(0.01, min(deadlines) - time.monotonic())
                timeout = min(timeout, 0.25)
                if stop_event is not None:
                    timeout = min(timeout, 0.25)
                try:
                    message = _receive_json(connection, timeout=timeout)
                except TimeoutError:
                    continue

                message_type = message.get("type")
                if not _valid_agent_command(message):
                    raise RuntimeError("Comando inválido recebido do servidor")
                if message_type == "heartbeat_ack":
                    validate_heartbeat_ack(message)
                    now = time.monotonic()
                    if now - last_inventory_at >= inventory_interval_seconds:
                        inventory_callback()
                        last_inventory_at = now
                elif message_type == "chat.message":
                    if client_app is not None and message["sender"] == "technician":
                        client_app.receive_chat(message["message"])
                elif message_type == "session.consent_request":
                    controller = consent if message["mode"] == "view" else control_consent
                    if (message["mode"] == "control" and not consent.is_active("view")) or not controller.request(
                        message["request_id"],
                        message["technician_name"],
                        message["mode"],
                        message["hostname"],
                        message["expires_in"],
                    ):
                        _send_json(
                            connection,
                            {
                                "type": "session.consent_response",
                                "request_id": message["request_id"],
                                "accepted": False,
                            },
                        )
                elif message_type == "session.consent_result":
                    if message["approved"]:
                        if not consent.activate(message["request_id"]):
                            control_consent.activate(message["request_id"])
                    else:
                        if control_consent.matches(message["request_id"]):
                            control_consent.stop()
                        elif consent.matches(message["request_id"]):
                            consent.stop()
                elif message_type == "session.remote_stop":
                    consent.stop()
                    control_consent.stop()
                    if remote_input is not None:
                        remote_input.stop()
                        remote_input = None
                    if capture is not None:
                        _close_capture(capture)
                        capture = None
                    screen_size = None
                    pending_frame = None
                    next_frame_at = None
                    _send_json(connection, {"type": "screen.stop_ack", "stopped": True})
                elif message_type == "screen.start":
                    if not consent.is_active("view"):
                        _send_json(connection, {"type":"screen.start_ack", "accepted":False, "reason":"not_authorized"})
                        continue
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
                        screen_size = getattr(
                            candidate, "screen_size", (pending_frame[1], pending_frame[2])
                        )
                        if (
                            not isinstance(screen_size, tuple)
                            or len(screen_size) != 2
                            or any(
                                isinstance(value, bool)
                                or not isinstance(value, int)
                                or not 1 <= value <= 32768
                                for value in screen_size
                            )
                        ):
                            raise ValueError("Resolução do monitor inválida")
                        capture = candidate
                        next_frame_at = time.monotonic()
                        _send_json(
                            connection,
                            {
                                "type": "screen.start_ack",
                                "accepted": True,
                                "width": pending_frame[1],
                                "height": pending_frame[2],
                                "screen_width": screen_size[0],
                                "screen_height": screen_size[1],
                                "max_fps": frame_rate,
                            },
                        )
                    except Exception:
                        consent.stop()
                        control_consent.stop()
                        if candidate is not None:
                            _close_capture(candidate)
                        capture = None
                        screen_size = None
                        pending_frame = None
                        next_frame_at = None
                        _send_json(
                            connection,
                            {"type": "screen.start_ack", "accepted": False, "reason": "capture_unavailable"},
                        )
                elif message_type == "screen.stop":
                    consent.stop()
                    control_consent.stop()
                    if remote_input is not None:
                        LOGGER.info("remote_control_stopped reason=screen_stopped")
                        remote_input.stop()
                        remote_input = None
                        _send_json(
                            connection,
                            {"type": "control.stop_ack", "stopped": True, "reason": "screen_stopped"},
                        )
                    if capture is not None:
                        _close_capture(capture)
                        capture = None
                    screen_size = None
                    pending_frame = None
                    next_frame_at = None
                    _send_json(connection, {"type": "screen.stop_ack", "stopped": True})
                elif message_type == "control.start":
                    LOGGER.info(
                        "remote_control_start_received capture_active=%s",
                        capture is not None and screen_size is not None,
                    )
                    if not consent.is_active("view") or not control_consent.is_active("control"):
                        _send_json(connection, {"type":"control.start_ack", "accepted":False, "reason":"not_authorized"})
                    elif remote_input is not None:
                        LOGGER.warning("remote_control_start_rejected reason=already_running")
                        _send_json(
                            connection,
                            {"type": "control.start_ack", "accepted": False, "reason": "already_running"},
                        )
                    elif capture is None or screen_size is None:
                        LOGGER.warning("remote_control_start_rejected reason=screen_not_running")
                        _send_json(
                            connection,
                            {"type": "control.start_ack", "accepted": False, "reason": "screen_not_running"},
                        )
                    else:
                        candidate_input = None
                        try:
                            factory = remote_input_factory or RemoteInputController
                            candidate_input = factory()
                            if hasattr(candidate_input, "set_authorization_check"):
                                candidate_input.set_authorization_check(
                                    lambda: consent.is_active("view") and control_consent.is_active("control")
                                )
                            candidate_input.start(*screen_size)
                            remote_input = candidate_input
                            LOGGER.info(
                                "remote_control_started width=%s height=%s",
                                screen_size[0],
                                screen_size[1],
                            )
                            _send_json(
                                connection,
                                {
                                    "type": "control.start_ack",
                                    "accepted": True,
                                    "screen_width": screen_size[0],
                                    "screen_height": screen_size[1],
                                },
                            )
                        except Exception as exc:
                            if candidate_input is not None:
                                candidate_input.stop()
                            control_consent.stop()
                            LOGGER.exception(
                                "remote_control_start_rejected reason=input_unavailable exception=%s",
                                type(exc).__name__,
                            )
                            _send_json(
                                connection,
                                {"type": "control.start_ack", "accepted": False, "reason": "input_unavailable"},
                            )
                elif message_type == "control.stop":
                    control_consent.stop()
                    if remote_input is not None:
                        LOGGER.info("remote_control_stopped reason=operator_request")
                        remote_input.stop()
                        remote_input = None
                    _send_json(connection, {"type": "control.stop_ack", "stopped": True})
                elif message_type == "control.input":
                    if remote_input is None or not control_consent.is_active("control") or not consent.is_active("view"):
                        _send_json(
                            connection,
                            {"type": "control.stop_ack", "stopped": True, "reason": "session_inactive"},
                        )
                    else:
                        try:
                            remote_input.apply(message)
                        except Exception as exc:
                            LOGGER.exception(
                                "remote_control_stopped reason=input_failed exception=%s",
                                type(exc).__name__,
                            )
                            remote_input.stop()
                            remote_input = None
                            _send_json(
                                connection,
                                {"type": "control.stop_ack", "stopped": True, "reason": "input_failed"},
                            )
                elif message_type == "session.stop":
                    consent.stop()
                    control_consent.stop()
                    if remote_input is not None:
                        LOGGER.info("remote_control_stopped reason=%s", message["reason"])
                        remote_input.stop()
                        remote_input = None
                        _send_json(
                            connection,
                            {"type": "control.stop_ack", "stopped": True, "reason": message["reason"]},
                        )
                    if capture is not None:
                        _close_capture(capture)
                        capture = None
                    screen_size = None
                    pending_frame = None
                    next_frame_at = None
                    _send_json(connection, {"type": "screen.stop_ack", "stopped": True})
                    break
                else:
                    raise RuntimeError("Mensagem não suportada recebida do servidor")

            if capture is not None:
                _close_capture(capture)
                capture = None
            consent.stop()
            control_consent.stop()
            if remote_input is not None:
                remote_input.stop()
                remote_input = None
            _send_json(connection, {"type": "disconnect"})
            try:
                _receive_json(connection, timeout=CLOSE_ACK_TIMEOUT_SECONDS)
            except TimeoutError:
                pass
    except InvalidStatus as exc:
        status = exc.response.status_code
        DIAGNOSTICS.warning("Canal recusado na abertura status_http=%d", status)
        raise RuntimeError(f"Canal recusado na abertura (HTTP {status})") from None
    except (OSError, TimeoutError, WebSocketException) as exc:
        DIAGNOSTICS.warning("Canal interrompido tipo=%s", type(exc).__name__)
        raise RuntimeError("Canal WebSocket indisponível") from None
    finally:
        consent.stop()
        control_consent.stop()
        if remote_input is not None:
            remote_input.stop()
        if capture is not None:
            _close_capture(capture)
        if client_app is not None:
            client_app.request_status(False)
            client_app.session_status("Compartilhamento inativo")
            while client_app.next_chat() is not None:
                pass

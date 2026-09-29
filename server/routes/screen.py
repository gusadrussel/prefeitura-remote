"""Ephemeral technician viewer for explicitly requested screen frames."""

import asyncio
import base64
import binascii
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.database.models import Computer
from server.routes.websocket import _is_secure_or_loopback

LOGGER = logging.getLogger("prefeitura_remote.screen")
screen_router = APIRouter()


def _is_authorized(websocket: WebSocket, computer_id: int, agent_id: str) -> bool:
    session: Session = websocket.app.state.session_factory()
    try:
        computer = session.scalar(
            select(Computer).where(
                Computer.id == computer_id,
                Computer.agent_id == agent_id,
                Computer.authorization_status == "authorized",
            )
        )
        return computer is not None
    finally:
        session.close()


def _valid_jpeg_frame(message: dict, settings) -> bool:
    data = message.get("data")
    width = message.get("width")
    height = message.get("height")
    sequence = message.get("sequence")
    if (
        message.get("format") != "jpeg"
        or not isinstance(data, str)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or not isinstance(sequence, int)
        or sequence < 0
        or width < 1
        or height < 1
        or width > settings.screen_max_width
        or height > settings.screen_max_height
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


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    await websocket.close(code=code, reason=reason)


@screen_router.websocket("/ws/admin/computers/{computer_id}/screen")
async def screen_viewer(websocket: WebSocket, computer_id: int) -> None:
    if not _is_secure_or_loopback(websocket):
        await _close(websocket, 1008, "WSS obrigatório fora de localhost")
        return

    ticket = websocket.query_params.get("ticket", "")
    streams = websocket.app.state.screen_streams
    agent_connections = websocket.app.state.agent_connections
    consumed = streams.consume_ticket(ticket, computer_id=computer_id)
    if consumed is None:
        await _close(websocket, 1008, "Ticket de visualização inválido ou expirado")
        return
    agent_id = consumed
    if not _is_authorized(websocket, computer_id, agent_id):
        await _close(websocket, 1008, "Computador não autorizado")
        return
    connection = agent_connections.get(agent_id)
    if not connection or not connection.connected or connection.computer_id != computer_id:
        await _close(websocket, 1008, "Agente desconectado")
        return

    viewer = streams.attach(agent_id, computer_id)
    if viewer is None:
        await _close(websocket, 1008, "Já existe um viewer para esta máquina")
        return

    await websocket.accept()
    receive_task = asyncio.create_task(websocket.receive_json())
    control_task = asyncio.create_task(viewer.control_queue.get())
    frame_task = asyncio.create_task(viewer.frame_queue.get())
    disconnected_task = asyncio.create_task(viewer.disconnected.wait())
    try:
        while True:
            done, _ = await asyncio.wait(
                {receive_task, control_task, frame_task, disconnected_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected_task in done and viewer.disconnected.is_set():
                await _close(websocket, 1001, "Canal do agente desconectado")
                break
            if receive_task in done:
                message = receive_task.result()
                receive_task = asyncio.create_task(websocket.receive_json())
                if not isinstance(message, dict):
                    await _close(websocket, 1003, "Mensagem inválida")
                    break

                message_type = message.get("type")
                if message_type == "screen.start":
                    if not _is_authorized(websocket, computer_id, agent_id):
                        streams.publish_control(
                            agent_id,
                            {"type": "screen.start_ack", "accepted": False, "reason": "not_authorized"},
                        )
                        continue
                    current = agent_connections.get(agent_id)
                    if not current or not current.connected:
                        streams.publish_control(
                            agent_id,
                            {"type": "screen.start_ack", "accepted": False, "reason": "agent_disconnected"},
                        )
                        continue
                    if not streams.request_start(viewer):
                        streams.publish_control(
                            agent_id,
                            {"type": "screen.start_ack", "accepted": False, "reason": "already_streaming"},
                        )
                        continue
                    settings = websocket.app.state.settings
                    command = {
                        "type": "screen.start",
                        "max_fps": max(1, min(settings.screen_max_fps, 5)),
                        "max_width": min(settings.screen_max_width, 1280),
                        "max_height": min(settings.screen_max_height, 720),
                        "jpeg_quality": max(30, min(settings.screen_jpeg_quality, 85)),
                        "max_frame_bytes": min(settings.screen_max_frame_bytes, 750_000),
                    }
                    if not agent_connections.send(agent_id, command):
                        streams.agent_started(
                            agent_id,
                            {"type": "screen.start_ack", "accepted": False, "reason": "agent_busy"},
                        )
                elif message_type == "screen.stop":
                    if streams.request_stop(viewer):
                        if not agent_connections.send(agent_id, {"type": "screen.stop"}):
                            streams.agent_stopped(
                                agent_id,
                                {"type": "screen.stop_ack", "stopped": True},
                            )
                    else:
                        streams.publish_control(
                            agent_id, {"type": "screen.stop_ack", "stopped": True}
                        )
                else:
                    await _close(websocket, 1003, "Mensagem não suportada no viewer")
                    break
            if control_task in done:
                await websocket.send_json(control_task.result())
                control_task = asyncio.create_task(viewer.control_queue.get())
            if frame_task in done:
                frame = frame_task.result()
                if viewer.state == "streaming":
                    await websocket.send_json(frame)
                frame_task = asyncio.create_task(viewer.frame_queue.get())
    except asyncio.CancelledError:
        LOGGER.debug("Viewer cancelado para a máquina %s", computer_id)
    except WebSocketDisconnect:
        pass
    except (RuntimeError, ValueError):
        LOGGER.exception("Erro no viewer da máquina %s", computer_id)
    finally:
        for task in (receive_task, control_task, frame_task, disconnected_task):
            task.cancel()
        await asyncio.gather(
            receive_task, control_task, frame_task, disconnected_task, return_exceptions=True
        )
        should_stop = streams.detach_and_stop_agent(viewer, agent_connections)
        if should_stop:
            LOGGER.info("Captura encerrada após desconexão do viewer: %s", agent_id)
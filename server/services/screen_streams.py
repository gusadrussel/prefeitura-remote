"""Volatile one-viewer screen bridge with bounded frame buffering."""

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock

SCREEN_TICKET_TTL_SECONDS = 30
MAX_PENDING_SCREEN_TICKETS = 1024
AUDIT_LOGGER = logging.getLogger("prefeitura_remote.audit")
TECHNICIAN_PRINCIPAL = "shared-dashboard-token"


class ScreenTicketCapacityError(RuntimeError):
    """Raised when the bounded store has no room for another viewer ticket."""


@dataclass(frozen=True)
class ScreenViewerTicket:
    agent_id: str
    computer_id: int
    expires_at: float


@dataclass
class ScreenViewer:
    agent_id: str
    computer_id: int
    control_queue: asyncio.Queue[dict] = field(
        default_factory=lambda: asyncio.Queue(maxsize=8)
    )
    frame_queue: asyncio.Queue[dict] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1)
    )
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    state: str = "idle"
    dropped_frames: int = 0
    control_state: str = "idle"
    control_session_id: str | None = None
    control_started: bool = False
    screen_width: int | None = None
    screen_height: int | None = None


class ScreenStreamRegistry:
    def __init__(self) -> None:
        self._tickets: dict[str, ScreenViewerTicket] = {}
        self._ticket_lock = Lock()
        self._viewers: dict[str, ScreenViewer] = {}

    def issue_ticket(
        self, agent_id: str, computer_id: int, ttl_seconds: int = SCREEN_TICKET_TTL_SECONDS
    ) -> tuple[str, int]:
        now = time.monotonic()
        with self._ticket_lock:
            self._purge_expired_tickets(now)
            if len(self._tickets) >= MAX_PENDING_SCREEN_TICKETS:
                raise ScreenTicketCapacityError("Limite de tickets pendentes atingido")
            token = secrets.token_urlsafe(32)
            self._tickets[token] = ScreenViewerTicket(
                agent_id=agent_id,
                computer_id=computer_id,
                expires_at=now + ttl_seconds,
            )
            return token, ttl_seconds

    def consume_ticket(self, token: str, computer_id: int) -> str | None:
        now = time.monotonic()
        with self._ticket_lock:
            self._purge_expired_tickets(now)
            ticket = self._tickets.pop(token, None)
            if ticket is None or ticket.expires_at <= now or ticket.computer_id != computer_id:
                return None
            return ticket.agent_id

    def _purge_expired_tickets(self, now: float) -> None:
        expired = [token for token, ticket in self._tickets.items() if ticket.expires_at <= now]
        for token in expired:
            self._tickets.pop(token, None)

    def attach(self, agent_id: str, computer_id: int) -> ScreenViewer | None:
        current = self._viewers.get(agent_id)
        if current is not None and not current.disconnected.is_set():
            return None
        viewer = ScreenViewer(agent_id=agent_id, computer_id=computer_id)
        self._viewers[agent_id] = viewer
        return viewer

    def detach(self, viewer: ScreenViewer) -> bool:
        if self._viewers.get(viewer.agent_id) is not viewer:
            return False
        self._viewers.pop(viewer.agent_id, None)
        viewer.disconnected.set()
        self.finish_control(viewer, "viewer_disconnected")
        return viewer.state in {"starting", "streaming", "stopping"}

    def detach_and_stop_agent(self, viewer: ScreenViewer, agent_connections) -> bool:
        should_stop = self.detach(viewer)
        if should_stop:
            agent_connections.send_urgent(viewer.agent_id, {"type": "screen.stop"})
        return should_stop

    def get(self, agent_id: str) -> ScreenViewer | None:
        return self._viewers.get(agent_id)

    def request_start(self, viewer: ScreenViewer) -> bool:
        if viewer.state != "idle" or viewer.disconnected.is_set():
            return False
        viewer.state = "starting"
        return True

    def request_stop(self, viewer: ScreenViewer) -> bool:
        if viewer.state == "idle" or viewer.disconnected.is_set():
            return False
        viewer.state = "stopping"
        self._discard_pending_frames(viewer)
        return True

    @staticmethod
    def _discard_pending_frames(viewer: ScreenViewer) -> None:
        while not viewer.frame_queue.empty():
            try:
                viewer.frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def publish_control(self, agent_id: str, message: dict) -> bool:
        viewer = self._viewers.get(agent_id)
        if viewer is None or viewer.disconnected.is_set():
            return False
        try:
            viewer.control_queue.put_nowait(message)
        except asyncio.QueueFull:
            viewer.disconnected.set()
            return False
        return True

    def publish_frame(self, agent_id: str, message: dict) -> bool:
        viewer = self._viewers.get(agent_id)
        if viewer is None or viewer.state != "streaming" or viewer.disconnected.is_set():
            return False
        if viewer.frame_queue.full():
            try:
                viewer.frame_queue.get_nowait()
                viewer.dropped_frames += 1
            except asyncio.QueueEmpty:
                pass
        viewer.frame_queue.put_nowait(message)
        return True

    def agent_started(self, agent_id: str, message: dict) -> bool:
        viewer = self._viewers.get(agent_id)
        if viewer is None or viewer.state != "starting":
            return False
        viewer.state = "streaming" if message.get("accepted") is True else "idle"
        viewer.screen_width = message.get("screen_width") if message.get("accepted") else None
        viewer.screen_height = message.get("screen_height") if message.get("accepted") else None
        return self.publish_control(agent_id, message)

    def agent_stopped(self, agent_id: str, message: dict) -> bool:
        viewer = self._viewers.get(agent_id)
        if viewer is None:
            return False
        viewer.state = "idle"
        viewer.screen_width = None
        viewer.screen_height = None
        self._discard_pending_frames(viewer)
        if viewer.control_state != "idle":
            self.finish_control(viewer, "screen_stopped")
            self.publish_control(
                agent_id,
                {"type": "control.stop_ack", "stopped": True, "reason": "screen_stopped"},
            )
        return self.publish_control(agent_id, message)

    def request_control_start(self, viewer: ScreenViewer) -> bool:
        if (
            viewer.state != "streaming"
            or viewer.control_state != "idle"
            or viewer.disconnected.is_set()
        ):
            return False
        viewer.control_state = "starting"
        viewer.control_session_id = uuid.uuid4().hex
        viewer.control_started = False
        return True

    def agent_control_started(self, agent_id: str, message: dict) -> bool:
        viewer = self._viewers.get(agent_id)
        if viewer is None or viewer.control_state not in {"starting", "stopping"}:
            return False
        if message.get("accepted") is True:
            viewer.control_started = True
            AUDIT_LOGGER.info(
                "remote_control_started technician=%s computer_id=%s agent_id=%s session_id=%s",
                TECHNICIAN_PRINCIPAL,
                viewer.computer_id,
                viewer.agent_id,
                viewer.control_session_id,
            )
            if (
                message.get("screen_width") != viewer.screen_width
                or message.get("screen_height") != viewer.screen_height
            ):
                self.finish_control(viewer, "resolution_mismatch")
                self.publish_control(
                    agent_id,
                    {
                        "type": "control.start_ack",
                        "accepted": False,
                        "reason": "input_unavailable",
                    },
                )
                return False
            if viewer.control_state == "stopping":
                self.publish_control(
                    agent_id,
                    {
                        "type": "control.start_ack",
                        "accepted": False,
                        "reason": "screen_stopped",
                    },
                )
                return False
            viewer.control_state = "active"
        else:
            self.finish_control(viewer, "start_rejected")
        return self.publish_control(agent_id, message)

    def request_control_stop(self, viewer: ScreenViewer) -> bool:
        if viewer.control_state not in {"starting", "active"} or viewer.disconnected.is_set():
            return False
        viewer.control_state = "stopping"
        return True

    def agent_control_stopped(self, agent_id: str, message: dict) -> bool:
        viewer = self._viewers.get(agent_id)
        if viewer is None:
            return False
        reason = message.get("reason", "operator_request")
        self.finish_control(viewer, reason)
        return self.publish_control(agent_id, message)

    @staticmethod
    def finish_control(viewer: ScreenViewer, reason: str) -> None:
        if viewer.control_session_id is not None and viewer.control_started:
            AUDIT_LOGGER.info(
                "remote_control_ended technician=%s computer_id=%s agent_id=%s session_id=%s reason=%s",
                TECHNICIAN_PRINCIPAL,
                viewer.computer_id,
                viewer.agent_id,
                viewer.control_session_id,
                reason,
            )
        viewer.control_state = "idle"
        viewer.control_session_id = None
        viewer.control_started = False

    def agent_disconnected(self, agent_id: str) -> bool:
        viewer = self._viewers.get(agent_id)
        if viewer is None:
            return False
        viewer.state = "idle"
        viewer.disconnected.set()
        self.finish_control(viewer, "agent_disconnected")
        return True
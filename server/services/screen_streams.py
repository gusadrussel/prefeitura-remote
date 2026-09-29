"""Volatile one-viewer screen bridge with bounded frame buffering."""

import asyncio
import secrets
import time
from dataclasses import dataclass, field

SCREEN_TICKET_TTL_SECONDS = 30


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


class ScreenStreamRegistry:
    def __init__(self) -> None:
        self._tickets: dict[str, ScreenViewerTicket] = {}
        self._viewers: dict[str, ScreenViewer] = {}

    def issue_ticket(
        self, agent_id: str, computer_id: int, ttl_seconds: int = SCREEN_TICKET_TTL_SECONDS
    ) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        self._tickets[token] = ScreenViewerTicket(
            agent_id=agent_id,
            computer_id=computer_id,
            expires_at=time.monotonic() + ttl_seconds,
        )
        return token, ttl_seconds

    def consume_ticket(self, token: str, computer_id: int) -> str | None:
        ticket = self._tickets.pop(token, None)
        if ticket is None or ticket.expires_at < time.monotonic() or ticket.computer_id != computer_id:
            return None
        return ticket.agent_id

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
        return viewer.state in {"starting", "streaming", "stopping"}

    def detach_and_stop_agent(self, viewer: ScreenViewer, agent_connections) -> bool:
        should_stop = self.detach(viewer)
        if should_stop:
            agent_connections.send(viewer.agent_id, {"type": "screen.stop"})
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
        return self.publish_control(agent_id, message)

    def agent_stopped(self, agent_id: str, message: dict) -> bool:
        viewer = self._viewers.get(agent_id)
        if viewer is None:
            return False
        viewer.state = "idle"
        self._discard_pending_frames(viewer)
        return self.publish_control(agent_id, message)

    def agent_disconnected(self, agent_id: str) -> bool:
        viewer = self._viewers.get(agent_id)
        if viewer is None:
            return False
        viewer.state = "idle"
        viewer.disconnected.set()
        return True
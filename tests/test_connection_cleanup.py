import asyncio
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import WebSocketDisconnect
from server.routes import screen, websocket
from server.services.agent_connections import AgentConnectionRegistry
from server.services.remote_consent import RemoteConsentRegistry
from server.services.screen_streams import ScreenStreamRegistry
from server.services.task_cleanup import cancel_tasks


class ConnectionCleanupTests(unittest.IsolatedAsyncioTestCase):
    def state(self):
        return SimpleNamespace(
            agent_connections=AgentConnectionRegistry(),
            screen_streams=ScreenStreamRegistry(), remote_consent=RemoteConsentRegistry(),
            settings=SimpleNamespace(agent_hello_timeout_seconds=1,
                agent_heartbeat_interval_seconds=30, agent_heartbeat_timeout_seconds=90, dashboard_token="fake"),
            rate_limiter=SimpleNamespace(reset=lambda key: None),
        )

    async def test_agent_cleanup_survives_cancellation_at_task_drain(self):
        state = self.state()
        socket = SimpleNamespace(app=SimpleNamespace(state=state),
            client=SimpleNamespace(host="127.0.0.1"), accept=AsyncMock(), send_json=AsyncMock(),
            receive_json=AsyncMock(side_effect=[{"type":"hello", "agent_id":"a"}, {"type":"disconnect"}]))
        original = websocket.cancel_tasks
        async def interrupted(*tasks):
            await original(*tasks)
            raise asyncio.CancelledError()
        with patch.object(websocket, "_is_secure_or_loopback", return_value=True), \
             patch.object(websocket, "_websocket_connection_allowed", return_value=True), \
             patch.object(websocket, "_credential_is_valid", return_value=True), \
             patch.object(websocket, "_find_authorized_computer", return_value=SimpleNamespace(id=1)), \
             patch.object(websocket, "cancel_tasks", side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await websocket.agent_channel(socket, "a")
        self.assertFalse(state.agent_connections.get("a").connected)

    async def test_viewer_accept_cancellation_detaches_viewer(self):
        state = self.state()
        state.agent_connections.connect("a", 1)
        ticket, _ = state.screen_streams.issue_ticket("a", 1)
        consent = state.remote_consent.create("a", 1, "Test", principal="dashboard-admin", bootstrap=True)
        state.remote_consent.respond(consent.request_id, "a", 1, True)
        state.remote_consent.attach_ticket(consent.request_id, ticket, 30)
        socket = SimpleNamespace(app=SimpleNamespace(state=state), query_params={"ticket":ticket},
                                 accept=AsyncMock(side_effect=asyncio.CancelledError()))
        with patch.object(screen, "_is_secure_or_loopback", return_value=True), \
             patch.object(screen, "_websocket_connection_allowed", return_value=True), \
             patch.object(screen, "_is_authorized", return_value=True):
            with self.assertRaises(asyncio.CancelledError):
                await screen.screen_viewer(socket, 1)
        self.assertIsNone(state.screen_streams.get("a"))

    async def test_duplicate_handshake_does_not_disconnect_existing_viewer(self):
        state = self.state()
        connection = state.agent_connections.connect("a", 1)
        viewer = state.screen_streams.attach("a", 1)
        socket = SimpleNamespace(app=SimpleNamespace(state=state),client=SimpleNamespace(host="127.0.0.1"),
            accept=AsyncMock(), close=AsyncMock(), receive_json=AsyncMock(return_value={"type":"hello","agent_id":"a"}))
        with patch.object(websocket, "_is_secure_or_loopback", return_value=True), \
             patch.object(websocket, "_websocket_connection_allowed", return_value=True), \
             patch.object(websocket, "_credential_is_valid", return_value=True), \
             patch.object(websocket, "_find_authorized_computer", return_value=SimpleNamespace(id=1)):
            await websocket.agent_channel(socket, "a")
        self.assertTrue(connection.connected)
        self.assertFalse(viewer.disconnected.is_set())

    async def test_owned_tasks_are_cancelled_and_drained(self):
        tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(3)]
        await cancel_tasks(*tasks)
        self.assertTrue(all(task.cancelled() for task in tasks))


class ImportIsolationTests(unittest.TestCase):
    def test_import_does_not_initialize_persistence(self):
        from server import main
        with patch("server.database.session.create_database", side_effect=AssertionError("database access")):
            importlib.reload(main)
            self.assertFalse(hasattr(main.app.state, "engine"))
        importlib.reload(main)

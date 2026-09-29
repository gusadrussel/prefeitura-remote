import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.config import Settings
from server.main import create_app
from server.routes.websocket import derive_agent_channel_credential


class AgentWebSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "websocket-test.db"
        settings = Settings(
            database_url=f"sqlite:///{database_path}",
            agent_token="agent-channel-token",
            dashboard_token="dashboard-test-token",
            online_threshold_seconds=900,
            agent_heartbeat_interval_seconds=0.02,
            agent_heartbeat_timeout_seconds=0.15,
            agent_hello_timeout_seconds=0.2,
        )
        self.app = create_app(settings)
        self.client = TestClient(self.app, client=("127.0.0.1", 50000))
        self.dashboard_headers = {"Authorization": "Bearer dashboard-test-token"}

    def tearDown(self):
        self.client.close()
        self.app.state.engine.dispose()
        self.temp_dir.cleanup()

    def _register_agent(self, hostname="WS-TEST"):
        payload = {
            "agent_id": "c0c0c0c0-1111-4222-8333-123456789abc",
            "system": {"hostname": hostname, "os_name": "Linux"},
        }
        response = self.client.post(
            "/api/v1/inventory",
            json=payload,
            headers={"Authorization": "Bearer agent-channel-token"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return payload["agent_id"], response.json()["computer_id"]

    def _authorize(self, computer_id):
        response = self.client.post(
            f"/api/v1/admin/computers/{computer_id}/authorize",
            headers=self.dashboard_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _connect(self, agent_id, token="agent-channel-token", host="127.0.0.1"):
        credential = derive_agent_channel_credential(agent_id, token)
        url = f"ws://{host}/ws/agents/{agent_id}"
        return self.client.websocket_connect(
            url, headers={"Authorization": f"Bearer {credential}"}
        )

    def test_pending_and_revoked_agents_are_rejected(self):
        agent_id, computer_id = self._register_agent()
        unknown_agent_id = "d0d0d0d0-1111-4222-8333-123456789abc"
        with self.assertRaises(WebSocketDisconnect):
            with self._connect(unknown_agent_id):
                pass
        with self.assertRaises(WebSocketDisconnect):
            with self._connect(agent_id):
                pass
        self.assertIsNone(self.app.state.agent_connections.get(agent_id))

        self._authorize(computer_id)
        revoked = self.client.post(
            f"/api/v1/admin/computers/{computer_id}/revoke",
            headers=self.dashboard_headers,
        )
        self.assertEqual(revoked.status_code, 200)
        with self.assertRaises(WebSocketDisconnect):
            with self._connect(agent_id):
                pass
        self.assertIsNone(self.app.state.agent_connections.get(agent_id))

    def test_invalid_agent_credential_and_non_loopback_ws_are_rejected(self):
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)
        with self.assertRaises(WebSocketDisconnect):
            with self._connect(agent_id, token="wrong-token"):
                pass
        with self.assertRaises(WebSocketDisconnect):
            with self._connect(agent_id, token="dashboard-test-token"):
                pass
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(
                f"ws://127.0.0.1/ws/agents/{agent_id}",
                headers={"Authorization": "Bearer agent-channel-token"},
            ):
                pass
        with self.assertRaises(WebSocketDisconnect):
            with self._connect(agent_id, host="remote.example.gov"):
                pass
        self.assertIsNone(self.app.state.agent_connections.get(agent_id))

    def test_remote_source_cannot_spoof_localhost_host(self):
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)
        remote_client = TestClient(self.app, client=("198.51.100.42", 50001))
        try:
            with self.assertRaises(WebSocketDisconnect):
                with remote_client.websocket_connect(
                    f"ws://localhost/ws/agents/{agent_id}",
                    headers={"Authorization": "Bearer agent-channel-token"},
                ):
                    pass
        finally:
            remote_client.close()
        self.assertIsNone(self.app.state.agent_connections.get(agent_id))

    def test_authorized_connection_hello_heartbeat_and_disconnect(self):
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)
        with self._connect(agent_id) as websocket:
            websocket.send_json({"type": "hello", "agent_id": agent_id, "version": "0.1.0"})
            hello_ack = websocket.receive_json()
            self.assertEqual(hello_ack["type"], "hello_ack")
            self.assertEqual(hello_ack["agent_id"], agent_id)

            connection = self.app.state.agent_connections.get(agent_id)
            self.assertTrue(connection.connected)
            self.assertEqual(connection.computer_id, computer_id)

            listing = self.client.get(
                "/api/v1/admin/computers", headers=self.dashboard_headers
            ).json()
            connected_computer = next(item for item in listing if item["agent_id"] == agent_id)
            self.assertTrue(connected_computer["channel_connected"])
            self.assertIsNotNone(connected_computer["channel_connected_at"])
            self.assertIsNotNone(connected_computer["channel_last_heartbeat"])

            websocket.send_json({"type": "heartbeat"})
            heartbeat_ack = websocket.receive_json()
            self.assertEqual(heartbeat_ack["type"], "heartbeat_ack")
            self.assertIn("timestamp", heartbeat_ack)

            websocket.send_json({"type": "disconnect"})
            self.assertEqual(websocket.receive_json()["type"], "disconnect_ack")

        connection = self.app.state.agent_connections.get(agent_id)
        self.assertFalse(connection.connected)
        listing = self.client.get(
            "/api/v1/admin/computers", headers=self.dashboard_headers
        ).json()
        disconnected_computer = next(item for item in listing if item["agent_id"] == agent_id)
        self.assertFalse(disconnected_computer["channel_connected"])

    def test_missing_heartbeat_disconnects_session(self):
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)
        with self._connect(agent_id) as websocket:
            websocket.send_json({"type": "hello", "agent_id": agent_id})
            self.assertEqual(websocket.receive_json()["type"], "hello_ack")
            with self.assertRaises(WebSocketDisconnect):
                websocket.receive_json()
        connection = self.app.state.agent_connections.get(agent_id)
        self.assertIsNotNone(connection)
        self.assertFalse(connection.connected)

    def test_revoking_agent_closes_its_active_session_on_next_heartbeat(self):
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)
        with self._connect(agent_id) as websocket:
            websocket.send_json({"type": "hello", "agent_id": agent_id})
            self.assertEqual(websocket.receive_json()["type"], "hello_ack")
            revoked = self.client.post(
                f"/api/v1/admin/computers/{computer_id}/revoke",
                headers=self.dashboard_headers,
            )
            self.assertEqual(revoked.status_code, 200)
            websocket.send_json({"type": "heartbeat"})
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        self.assertEqual(closed.exception.code, 1008)
        self.assertFalse(self.app.state.agent_connections.get(agent_id).connected)

    def test_unsupported_control_messages_are_not_implemented(self):
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)
        for message_type in (
            "screen.start",
            "screen.frame",
            "input.mouse",
            "input.keyboard",
            "exec.command",
            "file.transfer",
        ):
            with self.subTest(message_type=message_type):
                with self._connect(agent_id) as websocket:
                    websocket.send_json({"type": "hello", "agent_id": agent_id})
                    self.assertEqual(websocket.receive_json()["type"], "hello_ack")
                    websocket.send_json({"type": message_type})
                    with self.assertRaises(WebSocketDisconnect) as closed:
                        websocket.receive_json()
                self.assertEqual(closed.exception.code, 1003)
        connection = self.app.state.agent_connections.get(agent_id)
        self.assertFalse(connection.connected)


if __name__ == "__main__":
    unittest.main()
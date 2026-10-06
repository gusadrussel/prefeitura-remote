import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.config import Settings
from server.main import create_app
from server.provision_agent import provision_agent, revoke_agent_credential
from server.routes.websocket import derive_agent_channel_credential


class AgentWebSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "websocket-test.db"
        settings = Settings(
            database_url=f"sqlite:///{database_path}",
            agent_token="agent-channel-token",
            dashboard_token="dashboard-test-token",
            allow_legacy_agent_token=True,
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

    def _register_agent(
        self,
        hostname="WS-TEST",
        agent_id="c0c0c0c0-1111-4222-8333-123456789abc",
    ):
        payload = {
            "agent_id": agent_id,
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

    def _connect_with_credential(self, agent_id, credential):
        return self.client.websocket_connect(
            f"ws://127.0.0.1/ws/agents/{agent_id}",
            headers={"Authorization": f"Bearer {credential}"},
        )

    def _issue_individual_credential(self, agent_id):
        session = self.app.state.session_factory()
        try:
            _, credential = provision_agent(session, agent_id)
            return credential
        finally:
            session.close()

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

    def test_global_agent_token_fallback_requires_explicit_compatibility_flag(self):
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)
        self.app.state.settings = replace(
            self.app.state.settings,
            allow_legacy_agent_token=False,
        )

        with self.assertRaises(WebSocketDisconnect):
            with self._connect(agent_id):
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

    def test_repeated_agent_websocket_connections_are_limited_per_peer_and_agent(self):
        self.app.state.settings = replace(
            self.app.state.settings,
            websocket_connections_per_window=1,
            rate_limit_window_seconds=60,
        )
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)

        with self._connect(agent_id) as websocket:
            websocket.send_json({"type": "hello", "agent_id": agent_id, "version": "test"})
            self.assertEqual(websocket.receive_json()["type"], "hello_ack")
            websocket.send_json({"type": "disconnect"})
            self.assertEqual(websocket.receive_json()["type"], "disconnect_ack")

        with self.assertRaises(WebSocketDisconnect):
            with self._connect(agent_id):
                pass

    def test_invalid_agent_websocket_credentials_are_limited_per_peer(self):
        self.app.state.settings = replace(
            self.app.state.settings,
            auth_failures_per_window=1,
            rate_limit_window_seconds=60,
        )
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)

        with self.assertRaises(WebSocketDisconnect):
            with self._connect(agent_id, token="wrong-token"):
                pass
        with self.assertRaises(WebSocketDisconnect):
            with self._connect(agent_id, token="wrong-token"):
                pass

        failure_window = self.app.state.rate_limiter._windows[
            "websocket-auth-failure:127.0.0.1"
        ]
        self.assertEqual(failure_window.attempts, 1)

        with self._connect(agent_id) as websocket:
            websocket.send_json({"type": "hello", "agent_id": agent_id, "version": "test"})
            self.assertEqual(websocket.receive_json()["type"], "hello_ack")
            websocket.send_json({"type": "disconnect"})
            self.assertEqual(websocket.receive_json()["type"], "disconnect_ack")
        self.assertNotIn(
            "websocket-auth-failure:127.0.0.1",
            self.app.state.rate_limiter._windows,
        )

    def test_individual_credential_authenticates_and_revocation_closes_live_channel(self):
        agent_id, computer_id = self._register_agent()
        self._authorize(computer_id)
        credential = self._issue_individual_credential(agent_id)

        with self._connect_with_credential(agent_id, credential) as websocket:
            websocket.send_json({"type": "hello", "agent_id": agent_id, "version": "test"})
            self.assertEqual(websocket.receive_json()["type"], "hello_ack")
            session = self.app.state.session_factory()
            try:
                self.assertTrue(revoke_agent_credential(session, agent_id))
            finally:
                session.close()
            websocket.send_json({"type": "heartbeat"})
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        self.assertEqual(closed.exception.code, 1008)
        self.assertFalse(self.app.state.agent_connections.get(agent_id).connected)

    def test_individual_credential_cannot_impersonate_another_agent(self):
        first_agent, first_computer = self._register_agent(
            hostname="WS-FIRST",
            agent_id="c0c0c0c0-1111-4222-8333-123456789abc",
        )
        second_agent, second_computer = self._register_agent(
            hostname="WS-SECOND",
            agent_id="d0d0d0d0-1111-4222-8333-123456789abc",
        )
        self._authorize(first_computer)
        self._authorize(second_computer)
        first_credential = self._issue_individual_credential(first_agent)
        self._issue_individual_credential(second_agent)

        with self.assertRaises(WebSocketDisconnect):
            with self._connect_with_credential(second_agent, first_credential):
                pass
        self.assertIsNone(self.app.state.agent_connections.get(second_agent))

    def test_revoking_agent_sends_immediate_session_stop(self):
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
            self.assertEqual(
                websocket.receive_json(),
                {"type": "session.stop", "reason": "authorization_revoked"},
            )
            websocket.send_json({"type": "disconnect"})
            self.assertEqual(websocket.receive_json()["type"], "disconnect_ack")
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
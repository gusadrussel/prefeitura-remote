import base64
import json
import tempfile
import unittest
from types import SimpleNamespace
import uuid
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect
from unittest.mock import patch

from agent.connection import derive_channel_credential
from agent.screen_capture import encode_rgb_frame
from server.config import Settings
from server.main import create_app
from server.routes.websocket import _valid_screen_frame
from server.provision_agent import provision_agent, revoke_agent_credential


class ScreenApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "screen-test.db"
        self.settings = Settings(
            database_url=f"sqlite:///{database_path}",
            agent_token="agent-screen-token",
            dashboard_token="dashboard-screen-token",
            allow_legacy_agent_token=True,
            online_threshold_seconds=900,
            agent_heartbeat_interval_seconds=0.05,
            agent_heartbeat_timeout_seconds=5,
            agent_hello_timeout_seconds=0.2,
            screen_max_fps=3,
            screen_max_width=640,
            screen_max_height=360,
            screen_jpeg_quality=50,
            screen_max_frame_bytes=750_000,
        )
        self.app = create_app(self.settings)
        self.client = TestClient(self.app, client=("127.0.0.1", 50000))
        self.client.__enter__()
        self.dashboard_headers = {"Authorization": "Bearer dashboard-screen-token"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.app.state.engine.dispose()
        self.temp_dir.cleanup()

    def _register(self, authorization_status=None):
        agent_id = str(uuid.uuid4())
        payload = {
            "agent_id": agent_id,
            "system": {"hostname": "SCREEN-TEST", "os_name": "Windows"},
        }
        response = self.client.post(
            "/api/v1/inventory",
            json=payload,
            headers={"Authorization": "Bearer agent-screen-token"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        computer_id = response.json()["computer_id"]
        if authorization_status == "authorized":
            authorized = self.client.post(
                f"/api/v1/admin/computers/{computer_id}/authorize",
                headers=self.dashboard_headers,
            )
            self.assertEqual(authorized.status_code, 200)
        elif authorization_status == "revoked":
            self.client.post(
                f"/api/v1/admin/computers/{computer_id}/authorize",
                headers=self.dashboard_headers,
            )
            revoked = self.client.post(
                f"/api/v1/admin/computers/{computer_id}/revoke",
                headers=self.dashboard_headers,
            )
            self.assertEqual(revoked.status_code, 200)
        return agent_id, computer_id

    def _agent_socket(self, agent_id):
        credential = derive_channel_credential(agent_id, "agent-screen-token")
        return self.client.websocket_connect(
            f"ws://127.0.0.1/ws/agents/{agent_id}",
            headers={"Authorization": f"Bearer {credential}"},
        )

    def _individual_agent_socket(self, agent_id, credential):
        return self.client.websocket_connect(
            f"ws://127.0.0.1/ws/agents/{agent_id}",
            headers={"Authorization": f"Bearer {credential}"},
        )

    def _provision_agent_credential(self, agent_id):
        session = self.app.state.session_factory()
        try:
            _, credential = provision_agent(session, agent_id)
            return credential
        finally:
            session.close()

    def _issue_consent_request(self, computer_id):
        return self.client.post(
            f"/api/v1/admin/computers/{computer_id}/screen/access-request",
            headers=self.dashboard_headers,
        )

    def _issue_ticket(self, computer_id):
        computer = self.app.state.session_factory()
        try:
            row = computer.get(self.app.state.computer_model, computer_id)
            ticket, expires_in = self.app.state.screen_streams.issue_ticket(row.agent_id, computer_id)
            return SimpleNamespace(status_code=200, headers={}, json=lambda: {"ticket": ticket, "expires_in": expires_in}, text="")
        finally:
            computer.close()

    def test_pending_or_disconnected_machine_cannot_request_remote_access(self):
        _, pending_id = self._register()
        pending = self._issue_consent_request(pending_id)
        self.assertEqual(pending.status_code, 403)

        _, authorized_id = self._register("authorized")
        disconnected = self._issue_consent_request(authorized_id)
        self.assertEqual(disconnected.status_code, 409)
        _, revoked_id = self._register("revoked")
        revoked = self._issue_consent_request(revoked_id)
        self.assertEqual(revoked.status_code, 403)
        self.assertEqual(
            self.client.post(
                f"/api/v1/admin/computers/{authorized_id}/screen/access-request"
            ).status_code,
            401,
        )

    def test_remote_access_requires_user_consent_before_ticket(self):
        agent_id, computer_id = self._register("authorized")
        with self._agent_socket(agent_id) as agent_socket:
            agent_socket.send_json({"type": "hello", "agent_id": agent_id, "version": "test"})
            self.assertEqual(agent_socket.receive_json()["type"], "hello_ack")
            response = self._issue_consent_request(computer_id)
            self.assertEqual(response.status_code, 202, response.text)
            request_id = response.json()["request_id"]
            command = agent_socket.receive_json()
            self.assertEqual(command["type"], "session.consent_request")
            self.assertEqual(command["request_id"], request_id)
            self.assertEqual(command["technician_name"], "Técnico da TI")

            pending = self.client.get(
                f"/api/v1/admin/computers/{computer_id}/screen/access-request/{request_id}",
                headers=self.dashboard_headers,
            )
            self.assertEqual(pending.json()["status"], "pending")
            agent_socket.send_json({"type": "session.consent_response", "request_id": request_id, "accepted": True})
            result = agent_socket.receive_json()
            self.assertEqual(result["type"], "session.consent_result")
            self.assertTrue(result["approved"])

            approved = self.client.get(
                f"/api/v1/admin/computers/{computer_id}/screen/access-request/{request_id}",
                headers=self.dashboard_headers,
            )
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json()["status"], "approved")
            ticket = approved.json()["ticket"]
            self.assertTrue(ticket)
            agent_socket.close()

            consumed_agent = self.app.state.screen_streams.consume_ticket(ticket, computer_id)
            self.assertEqual(consumed_agent, agent_id)

    def test_user_rejection_does_not_issue_viewer_ticket(self):
        agent_id, computer_id = self._register("authorized")
        with self._agent_socket(agent_id) as agent_socket:
            agent_socket.send_json({"type": "hello", "agent_id": agent_id, "version": "test"})
            self.assertEqual(agent_socket.receive_json()["type"], "hello_ack")
            response = self._issue_consent_request(computer_id)
            request_id = response.json()["request_id"]
            self.assertEqual(agent_socket.receive_json()["type"], "session.consent_request")
            agent_socket.send_json({"type": "session.consent_response", "request_id": request_id, "accepted": False})
            result = agent_socket.receive_json()
            self.assertFalse(result["approved"])
            status = self.client.get(
                f"/api/v1/admin/computers/{computer_id}/screen/access-request/{request_id}",
                headers=self.dashboard_headers,
            )
            self.assertEqual(status.json()["status"], "rejected")
            self.assertNotIn("ticket", status.json())

    def test_pending_machine_cannot_open_control_viewer_with_stale_ticket(self):
        agent_id, computer_id = self._register()
        connection = self.app.state.agent_connections.connect(agent_id, computer_id)
        ticket, _ = self.app.state.screen_streams.issue_ticket(agent_id, computer_id)

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(
                f"ws://127.0.0.1/ws/admin/computers/{computer_id}/screen?ticket={ticket}"
            ):
                pass

        self.assertIsNone(self.app.state.screen_streams.get(agent_id))
        self.assertTrue(connection.outbound.empty())

    def test_viewer_ticket_capacity_returns_429_without_evicting_valid_ticket(self):
        first_agent_id, first_computer_id = self._register("authorized")
        second_agent_id, second_computer_id = self._register("authorized")
        with patch("server.services.screen_streams.MAX_PENDING_SCREEN_TICKETS", 1):
            first_ticket, _ = self.app.state.screen_streams.issue_ticket(first_agent_id, first_computer_id)
            with self.assertRaises(Exception):
                self.app.state.screen_streams.issue_ticket(second_agent_id, second_computer_id)
            self.assertEqual(
                self.app.state.screen_streams.consume_ticket(first_ticket, first_computer_id),
                first_agent_id,
            )

    def test_remote_consent_rate_limit_preserves_authorization_checks(self):
        self.app.state.settings = replace(
            self.settings,
            remote_consent_requests_per_window=1,
            rate_limit_window_seconds=60,
        )
        agent_id, computer_id = self._register("authorized")
        self.app.state.agent_connections.connect(agent_id, computer_id)

        first = self._issue_consent_request(computer_id)
        self.assertEqual(first.status_code, 202)
        limited = self._issue_consent_request(computer_id)
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry-after", limited.headers)

        pending_agent_id, pending_computer_id = self._register()
        self.app.state.agent_connections.connect(pending_agent_id, pending_computer_id)
        self.assertEqual(self._issue_consent_request(pending_computer_id).status_code, 403)

    def test_repeated_viewer_websocket_handshakes_are_limited(self):
        self.app.state.settings = replace(
            self.settings,
            websocket_connections_per_window=1,
            rate_limit_window_seconds=60,
        )
        agent_id, computer_id = self._register("authorized")
        self.app.state.agent_connections.connect(agent_id, computer_id)

        first_ticket = self._issue_ticket(computer_id)
        self.assertEqual(first_ticket.status_code, 200)
        with self.client.websocket_connect(
            f"ws://127.0.0.1/ws/admin/computers/{computer_id}/screen?ticket={first_ticket.json()['ticket']}"
        ) as viewer_socket:
            viewer_socket.close()

        second_ticket = self._issue_ticket(computer_id)
        self.assertEqual(second_ticket.status_code, 200)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(
                f"ws://127.0.0.1/ws/admin/computers/{computer_id}/screen?ticket={second_ticket.json()['ticket']}"
            ):
                pass

    def test_authorized_viewer_ticket_and_explicit_start_stop_protocol(self):
        agent_id, computer_id = self._register("authorized")
        agent_connection = self.app.state.agent_connections.connect(agent_id, computer_id)
        ticket_response = self._issue_ticket(computer_id)
        self.assertEqual(ticket_response.status_code, 200, ticket_response.text)
        ticket = ticket_response.json()["ticket"]
        self.assertEqual(
            self.app.state.screen_streams.consume_ticket(ticket, computer_id), agent_id
        )
        viewer = self.app.state.screen_streams.attach(agent_id, computer_id)
        self.assertIsNotNone(viewer)
        self.assertEqual(viewer.state, "idle")
        self.assertTrue(agent_connection.outbound.empty())

        self.assertTrue(self.app.state.screen_streams.request_start(viewer))
        start_command = {"type": "screen.start", "max_fps": 3, "max_frame_bytes": 750_000}
        self.assertTrue(self.app.state.agent_connections.send(agent_id, start_command))
        self.assertEqual(agent_connection.outbound.get_nowait(), start_command)
        self.assertEqual(viewer.state, "starting")

        ack = {"type": "screen.start_ack", "accepted": True}
        self.assertTrue(self.app.state.screen_streams.agent_started(agent_id, ack))
        self.assertEqual(viewer.state, "streaming")
        self.assertEqual(viewer.control_queue.get_nowait(), ack)

        self.assertTrue(self.app.state.screen_streams.request_stop(viewer))
        stop_command = {"type": "screen.stop"}
        self.assertTrue(self.app.state.agent_connections.send(agent_id, stop_command))
        self.assertEqual(agent_connection.outbound.get_nowait(), stop_command)
        stop_ack = {"type": "screen.stop_ack", "stopped": True}
        self.assertTrue(self.app.state.screen_streams.agent_stopped(agent_id, stop_ack))
        self.assertEqual(viewer.state, "idle")
        self.assertEqual(viewer.control_queue.get_nowait(), stop_ack)

    def test_control_requires_streaming_and_forwards_only_active_input(self):
        agent_id, computer_id = self._register("authorized")
        with self._agent_socket(agent_id) as agent_socket:
            agent_socket.send_json({"type": "hello", "agent_id": agent_id, "version": "test"})
            self.assertEqual(agent_socket.receive_json()["type"], "hello_ack")
            ticket_response = self._issue_ticket(computer_id)
            self.assertEqual(ticket_response.status_code, 200)
            ticket = ticket_response.json()["ticket"]
            with self.client.websocket_connect(
                f"ws://127.0.0.1/ws/admin/computers/{computer_id}/screen?ticket={ticket}"
            ) as viewer_socket:
                viewer_socket.send_json({"type": "control.start"})
                refused = viewer_socket.receive_json()
                self.assertEqual(refused["type"], "control.start_ack")
                self.assertFalse(refused["accepted"])
                self.assertEqual(refused["reason"], "screen_not_running")

                viewer_socket.send_json(
                    {"type": "control.input", "action": "key.down", "key": "ctrl"}
                )
                with self.assertRaises(WebSocketDisconnect):
                    viewer_socket.receive_json()
            agent_socket.send_json({"type": "disconnect"})
            self.assertEqual(agent_socket.receive_json()["type"], "disconnect_ack")

    def test_control_round_trip_and_revocation_terminate_session(self):
        agent_id, computer_id = self._register("authorized")
        with self._agent_socket(agent_id) as agent_socket:
            agent_socket.send_json({"type": "hello", "agent_id": agent_id, "version": "test"})
            self.assertEqual(agent_socket.receive_json()["type"], "hello_ack")
            ticket_response = self._issue_ticket(computer_id)
            self.assertEqual(ticket_response.status_code, 200)
            ticket = ticket_response.json()["ticket"]
            with self.client.websocket_connect(
                f"ws://127.0.0.1/ws/admin/computers/{computer_id}/screen?ticket={ticket}"
            ) as viewer_socket:
                viewer_socket.send_json({"type": "screen.start"})
                self.assertEqual(agent_socket.receive_json()["type"], "screen.start")
                agent_socket.send_json(
                    {
                        "type": "screen.start_ack",
                        "accepted": True,
                        "width": 640,
                        "height": 360,
                        "screen_width": 1920,
                        "screen_height": 1080,
                        "max_fps": 3,
                    }
                )
                self.assertTrue(viewer_socket.receive_json()["accepted"])

                viewer_socket.send_json({"type": "control.start"})
                self.assertEqual(agent_socket.receive_json(), {"type": "control.start"})
                agent_socket.send_json(
                    {
                        "type": "control.start_ack",
                        "accepted": True,
                        "screen_width": 1920,
                        "screen_height": 1080,
                    }
                )
                self.assertTrue(viewer_socket.receive_json()["accepted"])
                input_message = {
                    "type": "control.input",
                    "action": "pointer.move",
                    "x": 0.25,
                    "y": 0.75,
                }
                viewer_socket.send_json(input_message)
                self.assertEqual(agent_socket.receive_json(), input_message)

                revoked = self.client.post(
                    f"/api/v1/admin/computers/{computer_id}/revoke",
                    headers=self.dashboard_headers,
                )
                self.assertEqual(revoked.status_code, 200)
                self.assertEqual(
                    agent_socket.receive_json(),
                    {"type": "session.stop", "reason": "authorization_revoked"},
                )
                agent_socket.send_json({"type": "disconnect"})
                self.assertEqual(agent_socket.receive_json()["type"], "disconnect_ack")
                with self.assertRaises(WebSocketDisconnect):
                    viewer_socket.receive_json()

    def test_individual_credential_revocation_stops_existing_viewer_control(self):
        agent_id, computer_id = self._register("authorized")
        individual_credential = self._provision_agent_credential(agent_id)
        with self._individual_agent_socket(agent_id, individual_credential) as agent_socket:
            agent_socket.send_json({"type": "hello", "agent_id": agent_id, "version": "test"})
            self.assertEqual(agent_socket.receive_json()["type"], "hello_ack")
            ticket_response = self._issue_ticket(computer_id)
            self.assertEqual(ticket_response.status_code, 200)
            ticket = ticket_response.json()["ticket"]
            with self.client.websocket_connect(
                f"ws://127.0.0.1/ws/admin/computers/{computer_id}/screen?ticket={ticket}"
            ) as viewer_socket:
                viewer_socket.send_json({"type": "screen.start"})
                self.assertEqual(agent_socket.receive_json()["type"], "screen.start")
                agent_socket.send_json(
                    {
                        "type": "screen.start_ack",
                        "accepted": True,
                        "width": 640,
                        "height": 360,
                        "screen_width": 1920,
                        "screen_height": 1080,
                        "max_fps": 3,
                    }
                )
                self.assertTrue(viewer_socket.receive_json()["accepted"])
                viewer_socket.send_json({"type": "control.start"})
                self.assertEqual(agent_socket.receive_json(), {"type": "control.start"})
                agent_socket.send_json(
                    {
                        "type": "control.start_ack",
                        "accepted": True,
                        "screen_width": 1920,
                        "screen_height": 1080,
                    }
                )
                self.assertTrue(viewer_socket.receive_json()["accepted"])

                session = self.app.state.session_factory()
                try:
                    self.assertTrue(revoke_agent_credential(session, agent_id))
                finally:
                    session.close()
                viewer_socket.send_json(
                    {"type": "control.input", "action": "key.down", "key": "ctrl"}
                )
                with self.assertRaises(WebSocketDisconnect):
                    viewer_socket.receive_json()
                self.assertEqual(
                    agent_socket.receive_json(),
                    {"type": "session.stop", "reason": "channel_unavailable"},
                )
            agent_socket.close()

    def test_invalid_jpeg_frame_is_rejected_and_not_forwarded(self):
        class FrameSettings:
            screen_max_width = 1280
            screen_max_height = 720
            screen_max_frame_bytes = 750_000

        valid_jpeg = encode_rgb_frame(Image.new("RGB", (2, 2), (10, 20, 30)).tobytes(), 2, 2)[0]
        valid_message = {
            "type": "screen.frame",
            "format": "jpeg",
            "data": base64.b64encode(valid_jpeg).decode("ascii"),
            "width": 2,
            "height": 2,
            "sequence": 1,
        }
        self.assertTrue(_valid_screen_frame(valid_message, FrameSettings()))
        self.assertFalse(_valid_screen_frame({**valid_message, "unexpected": True}, FrameSettings()))
        self.assertFalse(_valid_screen_frame({**valid_message, "sequence": True}, FrameSettings()))
        invalid_message = {**valid_message, "data": base64.b64encode(b"not-a-jpeg").decode("ascii")}
        self.assertFalse(_valid_screen_frame(invalid_message, FrameSettings()))
        oversized = {**valid_message, "width": 1281}
        self.assertFalse(_valid_screen_frame(oversized, FrameSettings()))


if __name__ == "__main__":
    unittest.main()
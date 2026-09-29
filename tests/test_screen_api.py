import base64
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect

from agent.connection import derive_channel_credential
from agent.screen_capture import encode_rgb_frame
from server.config import Settings
from server.main import create_app
from server.routes.websocket import _valid_screen_frame


class ScreenApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "screen-test.db"
        self.settings = Settings(
            database_url=f"sqlite:///{database_path}",
            agent_token="agent-screen-token",
            dashboard_token="dashboard-screen-token",
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

    def _issue_ticket(self, computer_id):
        return self.client.post(
            f"/api/v1/admin/computers/{computer_id}/screen/viewer-ticket",
            headers=self.dashboard_headers,
        )

    def test_pending_or_disconnected_machine_cannot_get_viewer_ticket(self):
        _, pending_id = self._register()
        pending = self._issue_ticket(pending_id)
        self.assertEqual(pending.status_code, 403)

        _, authorized_id = self._register("authorized")
        disconnected = self._issue_ticket(authorized_id)
        self.assertEqual(disconnected.status_code, 409)
        _, revoked_id = self._register("revoked")
        revoked = self._issue_ticket(revoked_id)
        self.assertEqual(revoked.status_code, 403)
        self.assertEqual(
            self.client.post(f"/api/v1/admin/computers/{authorized_id}/screen/viewer-ticket").status_code,
            401,
        )

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
        invalid_message = {**valid_message, "data": base64.b64encode(b"not-a-jpeg").decode("ascii")}
        self.assertFalse(_valid_screen_frame(invalid_message, FrameSettings()))
        oversized = {**valid_message, "width": 1281}
        self.assertFalse(_valid_screen_frame(oversized, FrameSettings()))


if __name__ == "__main__":
    unittest.main()
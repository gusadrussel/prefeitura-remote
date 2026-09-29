import json
import threading
import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from agent.connection import (
    derive_channel_credential,
    run_websocket_session,
    validate_heartbeat_ack,
    validate_hello_ack,
    websocket_url,
)
from server.routes.websocket import (
    derive_agent_channel_credential,
    heartbeat_ack_message,
    hello_ack_message,
)


class AgentWebSocketClientTests(unittest.TestCase):
    def test_channel_credential_is_agent_specific_and_matches_server(self):
        first = derive_channel_credential("agent-1", "shared-agent-secret")
        second = derive_channel_credential("agent-2", "shared-agent-secret")
        self.assertEqual(first, derive_agent_channel_credential("agent-1", "shared-agent-secret"))
        self.assertNotEqual(first, second)

    def test_server_hello_and_heartbeat_ack_payloads_pass_client_validation(self):
        hello_ack = hello_ack_message("agent-1", datetime.now(UTC), 30)
        heartbeat_ack = heartbeat_ack_message()
        self.assertEqual(hello_ack["type"], "hello_ack")
        self.assertEqual(heartbeat_ack["type"], "heartbeat_ack")
        validate_hello_ack(hello_ack, "agent-1")
        validate_heartbeat_ack(heartbeat_ack)
        self.assertIn("timestamp", heartbeat_ack)

    def test_agent_rejects_non_heartbeat_message_as_ack(self):
        with self.assertRaisesRegex(RuntimeError, "Resposta heartbeat_ack inválida"):
            validate_heartbeat_ack({"type": "screen.start"})

    def test_http_loopback_and_https_map_to_ws_and_wss(self):
        self.assertEqual(
            websocket_url("http://127.0.0.1:8000", "agent-1"),
            "ws://127.0.0.1:8000/ws/agents/agent-1",
        )
        self.assertEqual(
            websocket_url("https://central.example.gov/base", "agent-1"),
            "wss://central.example.gov/base/ws/agents/agent-1",
        )
        self.assertEqual(
            websocket_url("http://127.0.0.2:8000", "agent-1"),
            "ws://127.0.0.2:8000/ws/agents/agent-1",
        )
        with self.assertRaises(ValueError):
            websocket_url("http://central.example.gov", "agent-1")

    def test_client_authenticates_sends_hello_heartbeat_and_disconnect(self):
        stop_event = threading.Event()
        fake_connection = MagicMock()
        fake_connection.__enter__.return_value = fake_connection
        fake_connection.recv.side_effect = [
            json.dumps({"type": "hello_ack", "agent_id": "agent-1", "heartbeat_interval": 1}),
            json.dumps(heartbeat_ack_message()),
            json.dumps({"type": "disconnect_ack"}),
        ]

        def record_message(message):
            parsed = json.loads(message)
            if parsed["type"] == "heartbeat":
                stop_event.set()

        fake_connection.send.side_effect = record_message
        with patch("agent.connection.connect", return_value=fake_connection) as connect:
            run_websocket_session(
                "http://localhost:8000",
                "agent-1",
                "agent-token",
                300,
                lambda: None,
                stop_event=stop_event,
            )

        self.assertEqual(connect.call_args.args[0], "ws://localhost:8000/ws/agents/agent-1")
        self.assertEqual(
            connect.call_args.kwargs["additional_headers"],
            {
                "Authorization": "Bearer "
                + derive_channel_credential("agent-1", "agent-token")
            },
        )
        sent_messages = [json.loads(call.args[0])["type"] for call in fake_connection.send.call_args_list]
        self.assertEqual(sent_messages, ["hello", "heartbeat", "disconnect"])


if __name__ == "__main__":
    unittest.main()
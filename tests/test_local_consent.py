import json
from threading import Event
import unittest
from unittest.mock import MagicMock, patch

from agent.client_app import ClientApp
from agent.consent import ConsentController
from agent.connection import run_websocket_session
from tests.test_screen_capture import FakeScreenCapture


class LocalConsentTests(unittest.TestCase):
    def controller(self, mode="view"):
        controller = ConsentController()
        with patch("agent.consent.threading.Thread.start"):
            controller.request("request", "Test technician", mode, "TEST", 60)
        return controller

    def test_server_approval_without_local_acceptance_is_rejected(self):
        controller = self.controller()
        self.assertFalse(controller.activate("request"))
        self.assertFalse(controller.is_active("view"))

    def test_view_and_control_are_distinct_and_stop_revokes_immediately(self):
        controller = self.controller()
        controller._emit("accept", "request")
        self.assertFalse(controller.activate("other-request"))
        self.assertTrue(controller.activate("request"))
        self.assertTrue(controller.is_active("view"))
        self.assertFalse(controller.is_active("control"))
        controller._emit("user_stop", "request")
        self.assertFalse(controller.is_active("view"))

    def test_expired_and_rejected_requests_cannot_activate(self):
        controller = self.controller()
        controller._emit("reject", "request")
        self.assertFalse(controller.activate("request"))
        controller._deadline = 0
        controller._emit("accept", "request")
        self.assertFalse(controller.activate("request"))

    def test_main_window_end_request_closes_capture_before_server_notification(self):
        client = ClientApp("a", "http://127.0.0.1")
        capture = FakeScreenCapture()
        view = self.controller()
        view._emit("accept", "request")
        self.assertTrue(view.activate("request"))
        stop = Event()
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.side_effect = [
            '{"type":"hello_ack","agent_id":"a","heartbeat_interval":30}',
            '{"type":"screen.start","max_fps":1}',
            TimeoutError(),
            '{"type":"disconnect_ack"}',
        ]

        def send(raw):
            message = json.loads(raw)
            if message["type"] == "screen.start_ack":
                self.assertTrue(message["accepted"])
                client.end_session.set()
            if message["type"] == "session.user_stop":
                self.assertEqual(message["request_id"], "request")
                self.assertFalse(view.is_active("view"))
                self.assertEqual(capture.close_count, 1)
                stop.set()

        connection.send.side_effect = send
        with patch("agent.connection.connect", return_value=connection), \
             patch("agent.connection.ConsentController", side_effect=[view, ConsentController()]):
            run_websocket_session("http://127.0.0.1", "a", "fake-token", 300, lambda: None,
                                  stop_event=stop, screen_capture_factory=MagicMock(return_value=capture), client_app=client)
        self.assertTrue(stop.is_set())
        self.assertEqual(capture.close_count, 1)

    def test_agent_denies_capture_and_input_without_consent(self):
        stop = Event()
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.side_effect = [json.dumps(message) for message in [
            {"type":"hello_ack","agent_id":"a","heartbeat_interval":30},
            {"type":"session.consent_result","request_id":"forged","approved":True,"reason":"user_accepted"},
            {"type":"screen.start", "max_fps":1}, {"type":"control.start"},
            {"type":"disconnect_ack"},
        ]]
        def send(raw):
            if json.loads(raw)["type"] == "control.start_ack":
                stop.set()
        connection.send.side_effect = send
        capture = MagicMock()
        inputs = MagicMock()
        with patch("agent.connection.connect", return_value=connection):
            run_websocket_session("http://127.0.0.1", "a", "fake-token", 300, lambda:None,
                                  stop_event=stop, screen_capture_factory=capture, remote_input_factory=inputs)
        capture.assert_not_called()
        inputs.assert_not_called()
        refusals = [json.loads(call.args[0]) for call in connection.send.call_args_list
                    if json.loads(call.args[0])["type"] in {"screen.start_ack","control.start_ack"}]
        self.assertEqual(len(refusals), 2)
        self.assertTrue(all(not item["accepted"] for item in refusals))

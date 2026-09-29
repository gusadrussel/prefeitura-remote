import json
import unittest
from io import BytesIO
from threading import Event
from unittest.mock import MagicMock, patch

from PIL import Image

from agent.connection import run_websocket_session
from agent.screen_capture import encode_rgb_frame


class ScreenFrameEncodingTests(unittest.TestCase):
    def test_rgb_frame_is_jpeg_downscaled_and_bounded(self):
        image = Image.new("RGB", (640, 360), color=(80, 120, 160))
        rgb = image.tobytes()
        frame, width, height = encode_rgb_frame(
            rgb, 640, 360, max_width=320, max_height=180, max_bytes=100_000
        )
        decoded = Image.open(BytesIO(frame))
        self.assertEqual(frame[:2], b"\xff\xd8")
        self.assertLessEqual(len(frame), 100_000)
        self.assertEqual((width, height), (320, 180))
        self.assertEqual(decoded.size, (320, 180))

    def test_rejects_inconsistent_raw_frame_dimensions(self):
        with self.assertRaises(ValueError):
            encode_rgb_frame(b"rgb", 10, 10)


class FakeScreenCapture:
    def __init__(self, **options):
        self.options = options
        self.close_count = 0
        self.capture_count = 0

    def capture_jpeg(self):
        self.capture_count += 1
        return b"\xff\xd8fake-jpeg\xff\xd9", 2, 2

    def close(self):
        self.close_count += 1


class ScreenCaptureLifecycleTests(unittest.TestCase):
    def _fake_websocket(self, messages, on_send=None):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.side_effect = [json_message for json_message in messages]

        def send(message):
            if on_send:
                on_send(json_message(message))

        connection.send.side_effect = send
        return connection

    def _run_session(self, connection, capture_factory, stop_event):
        with patch("agent.connection.connect", return_value=connection):
            run_websocket_session(
                "http://127.0.0.1:8000",
                "agent-1",
                "agent-token",
                300,
                lambda: None,
                stop_event=stop_event,
                screen_capture_factory=capture_factory,
            )

    def test_capture_starts_only_on_request_and_stop_closes_it(self):
        stop_event = Event()
        capture = FakeScreenCapture()
        messages = [
            '{"type":"hello_ack","agent_id":"agent-1","heartbeat_interval":1}',
            '{"type":"heartbeat_ack"}',
            '{"type":"screen.start","max_fps":2}',
            '{"type":"heartbeat_ack"}',
            '{"type":"screen.stop"}',
            '{"type":"disconnect_ack"}',
        ]

        def on_send(message):
            if message["type"] == "screen.stop_ack":
                stop_event.set()

        connection = self._fake_websocket(messages, on_send)
        factory = MagicMock(return_value=capture)
        self._run_session(connection, factory, stop_event)

        sent = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertEqual(factory.call_count, 1)
        self.assertGreaterEqual(capture.capture_count, 1)
        self.assertEqual(capture.close_count, 1)
        self.assertIn("screen.frame", [message["type"] for message in sent])
        self.assertEqual(sent[-2]["type"], "screen.stop_ack")
        self.assertEqual(sent[-1]["type"], "disconnect")

    def test_connected_agent_does_not_capture_until_screen_start(self):
        stop_event = Event()
        messages = [
            '{"type":"hello_ack","agent_id":"agent-1","heartbeat_interval":1}',
            '{"type":"heartbeat_ack"}',
            '{"type":"disconnect_ack"}',
        ]
        connection = self._fake_websocket(
            messages,
            lambda message: stop_event.set() if message["type"] == "heartbeat" else None,
        )
        factory = MagicMock()
        self._run_session(connection, factory, stop_event)
        factory.assert_not_called()

    def test_websocket_failure_closes_active_capture(self):
        capture = FakeScreenCapture()
        messages = [
            '{"type":"hello_ack","agent_id":"agent-1","heartbeat_interval":1}',
            '{"type":"heartbeat_ack"}',
            '{"type":"screen.start","max_fps":1}',
            OSError("connection lost"),
        ]
        connection = self._fake_websocket(messages)
        with self.assertRaises(RuntimeError):
            self._run_session(connection, MagicMock(return_value=capture), Event())
        self.assertEqual(capture.close_count, 1)


def json_message(message):
    return json.loads(message)


if __name__ == "__main__":
    unittest.main()
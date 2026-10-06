import json
import time
import unittest
from io import BytesIO
from threading import Event
from unittest.mock import MagicMock, patch

from PIL import Image

from agent.connection import run_websocket_session
from agent.remote_input import RemoteInputController
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
        self.screen_size = (1920, 1080)

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

    def _run_session(self, connection, capture_factory, stop_event, remote_input=None):
        with patch("agent.connection.connect", return_value=connection):
            run_websocket_session(
                "http://127.0.0.1:8000",
                "agent-1",
                "agent-token",
                300,
                lambda: None,
                stop_event=stop_event,
                screen_capture_factory=capture_factory,
                remote_input_factory=(
                    remote_input if callable(remote_input) else lambda: remote_input
                ) if remote_input is not None else None,
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

    def test_websocket_failure_releases_control_and_closes_capture(self):
        class FakeRemoteInput:
            def __init__(self):
                self.stop_count = 0

            def start(self, width, height):
                pass

            def apply(self, message):
                pass

            def stop(self):
                self.stop_count += 1

        capture = FakeScreenCapture()
        remote_input = FakeRemoteInput()
        messages = [
            '{"type":"hello_ack","agent_id":"agent-1","heartbeat_interval":1}',
            '{"type":"heartbeat_ack"}',
            '{"type":"screen.start","max_fps":1}',
            '{"type":"control.start"}',
            OSError("connection lost"),
        ]
        connection = self._fake_websocket(messages)
        with self.assertRaises(RuntimeError):
            self._run_session(connection, MagicMock(return_value=capture), Event(), remote_input)
        self.assertEqual(capture.close_count, 1)
        self.assertEqual(remote_input.stop_count, 1)

    def test_authorization_revocation_command_releases_control_and_capture(self):
        class FakeRemoteInput:
            def __init__(self):
                self.stop_count = 0

            def start(self, width, height):
                pass

            def apply(self, message):
                pass

            def stop(self):
                self.stop_count += 1

        stop_event = Event()
        capture = FakeScreenCapture()
        remote_input = FakeRemoteInput()
        messages = [
            '{"type":"hello_ack","agent_id":"agent-1","heartbeat_interval":1}',
            '{"type":"heartbeat_ack"}',
            '{"type":"screen.start","max_fps":1}',
            '{"type":"control.start"}',
            '{"type":"session.stop","reason":"authorization_revoked"}',
            '{"type":"disconnect_ack"}',
        ]
        connection = self._fake_websocket(messages)
        self._run_session(connection, MagicMock(return_value=capture), stop_event, remote_input)

        sent = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertEqual(remote_input.stop_count, 1)
        self.assertEqual(capture.close_count, 1)
        self.assertIn("control.stop_ack", [message["type"] for message in sent])
        self.assertEqual(sent[-1]["type"], "disconnect")

    def test_control_runs_alongside_capture_and_stop_releases_backend(self):
        class FakeRemoteInput:
            def __init__(self):
                self.started_with = None
                self.messages = []
                self.stop_count = 0

            def start(self, width, height):
                self.started_with = (width, height)

            def apply(self, message):
                self.messages.append(message)
                time.sleep(0.55)

            def stop(self):
                self.stop_count += 1

        stop_event = Event()
        capture = FakeScreenCapture()
        remote_input = FakeRemoteInput()
        messages = [
            '{"type":"hello_ack","agent_id":"agent-1","heartbeat_interval":1}',
            '{"type":"heartbeat_ack"}',
            '{"type":"screen.start","max_fps":2}',
            '{"type":"heartbeat_ack"}',
            '{"type":"control.start"}',
            '{"type":"control.input","action":"key.down","key":"ctrl"}',
            '{"type":"control.stop"}',
            '{"type":"screen.stop"}',
            '{"type":"disconnect_ack"}',
        ]

        def on_send(message):
            if message["type"] == "screen.stop_ack":
                stop_event.set()

        connection = self._fake_websocket(messages, on_send)
        self._run_session(
            connection,
            MagicMock(return_value=capture),
            stop_event,
            remote_input,
        )

        sent = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertEqual(remote_input.started_with, (1920, 1080))
        self.assertEqual(remote_input.messages[0]["action"], "key.down")
        self.assertEqual(remote_input.stop_count, 1)
        self.assertGreater(capture.capture_count, 1)
        self.assertEqual(capture.close_count, 1)
        self.assertIn("screen.frame", [message["type"] for message in sent])
        self.assertIn("control.stop_ack", [message["type"] for message in sent])

    def test_missing_platform_input_dependency_is_reported_as_input_unavailable(self):
        stop_event = Event()
        capture = FakeScreenCapture()
        messages = [
            '{"type":"hello_ack","agent_id":"agent-1","heartbeat_interval":1}',
            '{"type":"heartbeat_ack"}',
            '{"type":"screen.start","max_fps":2}',
            '{"type":"control.start"}',
            '{"type":"disconnect_ack"}',
        ]

        def unavailable_input_factory():
            raise ModuleNotFoundError("No module named 'pyautogui'")

        def on_send(message):
            if message["type"] == "control.start_ack":
                stop_event.set()

        connection = self._fake_websocket(messages, on_send)
        with self.assertLogs("prefeitura_remote.agent.control", level="ERROR") as captured_logs:
            self._run_session(
                connection,
                MagicMock(return_value=capture),
                stop_event,
                remote_input=unavailable_input_factory,
            )

        sent = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        start_ack = next(item for item in sent if item["type"] == "control.start_ack")
        self.assertEqual(start_ack, {
            "type": "control.start_ack",
            "accepted": False,
            "reason": "input_unavailable",
        })
        self.assertIn("No module named 'pyautogui'", "\n".join(captured_logs.output))

    def test_slow_os_input_does_not_pause_screen_capture(self):
        input_started = Event()
        input_finished = Event()

        class SlowBackend:
            def keyDown(self, key):
                input_started.set()
                time.sleep(1.5)
                input_finished.set()

            def keyUp(self, key):
                pass

        class TrackingCapture(FakeScreenCapture):
            captures_while_input_busy = 0

            def capture_jpeg(self):
                frame = super().capture_jpeg()
                if input_started.is_set() and not input_finished.is_set():
                    self.captures_while_input_busy += 1
                return frame

        capture = TrackingCapture()
        remote_input = RemoteInputController(backend=SlowBackend())
        connection = self._fake_websocket([])
        responses = iter(
            [
                '{"type":"hello_ack","agent_id":"agent-1","heartbeat_interval":1}',
                '{"type":"heartbeat_ack"}',
                '{"type":"screen.start","max_fps":2}',
                '{"type":"heartbeat_ack"}',
                '{"type":"control.start"}',
                '{"type":"control.input","action":"key.down","key":"ctrl"}',
                None,
                '{"type":"control.stop"}',
                '{"type":"screen.stop"}',
                '{"type":"disconnect_ack"}',
            ]
        )

        def receive(*, timeout=None):
            response = next(responses)
            if response is None:
                time.sleep(0.55)
                raise TimeoutError()
            return response

        connection.recv.side_effect = receive
        stop_event = Event()

        def on_send(message):
            if message["type"] == "screen.stop_ack":
                stop_event.set()

        connection.send.side_effect = lambda message: on_send(json_message(message))
        self._run_session(connection, MagicMock(return_value=capture), stop_event, remote_input)

        sent = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertGreaterEqual(capture.captures_while_input_busy, 1)
        self.assertGreaterEqual(
            sum(message["type"] == "screen.frame" for message in sent), 2
        )
        self.assertTrue(input_finished.is_set())

    def test_capture_failure_ends_control_and_releases_held_keys(self):
        class FailingCapture(FakeScreenCapture):
            def capture_jpeg(self):
                self.capture_count += 1
                if self.capture_count > 1:
                    raise RuntimeError("capture failed")
                return b"\xff\xd8fake-jpeg\xff\xd9", 2, 2

        class FakeRemoteInput:
            def __init__(self):
                self.stop_count = 0

            def start(self, width, height):
                time.sleep(0.55)

            def apply(self, message):
                pass

            def stop(self):
                self.stop_count += 1

        stop_event = Event()
        capture = FailingCapture()
        remote_input = FakeRemoteInput()
        messages = [
            '{"type":"hello_ack","agent_id":"agent-1","heartbeat_interval":1}',
            '{"type":"heartbeat_ack"}',
            '{"type":"screen.start","max_fps":2}',
            '{"type":"control.start"}',
            TimeoutError(),
            '{"type":"disconnect_ack"}',
        ]

        def on_send(message):
            if message["type"] == "screen.stop_ack":
                stop_event.set()

        connection = self._fake_websocket(messages, on_send)
        self._run_session(
            connection,
            MagicMock(return_value=capture),
            stop_event,
            remote_input,
        )

        sent = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        control_ack = next(item for item in sent if item["type"] == "control.stop_ack")
        self.assertEqual(control_ack["reason"], "capture_failed")
        self.assertEqual(remote_input.stop_count, 1)
        self.assertEqual(capture.close_count, 1)


def json_message(message):
    return json.loads(message)


if __name__ == "__main__":
    unittest.main()
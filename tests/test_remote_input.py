import unittest
import time
from threading import Event
from unittest.mock import Mock

from agent.remote_input import RemoteInputController, valid_control_input


class RemoteInputControllerTests(unittest.TestCase):
    def test_control_input_requires_allowlisted_shape_and_values(self):
        valid = {"type": "control.input", "action": "key.down", "key": "a"}
        self.assertTrue(valid_control_input(valid))
        self.assertFalse(valid_control_input({**valid, "command": "anything"}))
        self.assertFalse(valid_control_input({**valid, "key": "f13"}))
        self.assertFalse(valid_control_input({**valid, "action": []}))
        self.assertFalse(
            valid_control_input(
                {"type": "control.input", "action": "pointer.move", "x": 1.1, "y": 0.5}
            )
        )

    def test_coordinates_map_to_real_screen_edges_and_stop_releases_inputs(self):
        processed = Event()
        backend = Mock()
        backend.keyDown.side_effect = lambda key: processed.set()
        controller = RemoteInputController(backend=backend)
        controller.start(1920, 1080)
        controller.apply(
            {"type": "control.input", "action": "pointer.move", "x": 1, "y": 1}
        )
        controller.apply(
            {
                "type": "control.input",
                "action": "pointer.down",
                "x": 0.5,
                "y": 0.5,
                "button": "right",
            }
        )
        controller.apply({"type": "control.input", "action": "key.down", "key": "ctrl"})
        self.assertTrue(processed.wait(1))
        controller.stop()

        backend.moveTo.assert_any_call(1919, 1079)
        backend.mouseDown.assert_called_once_with(button="right")
        backend.mouseUp.assert_called_once_with(button="right")
        backend.keyUp.assert_called_once_with("ctrl")
        with self.assertRaises(ValueError):
            controller.apply({"type": "control.input", "action": "key.down", "key": "a"})

    def test_real_backend_is_limited_to_windows(self):
        with self.assertRaisesRegex(RuntimeError, "somente no agente Windows"):
            RemoteInputController(platform_name="linux")

    def test_slow_platform_input_does_not_block_caller(self):
        entered = Event()
        release = Event()

        class SlowBackend:
            def moveTo(self, x, y):
                entered.set()
                release.wait(2)

        controller = RemoteInputController(backend=SlowBackend())
        controller.start(1920, 1080)
        started_at = time.monotonic()
        controller.apply(
            {"type": "control.input", "action": "pointer.move", "x": 0.5, "y": 0.5}
        )
        returned_quickly = time.monotonic() - started_at < 0.1
        self.assertTrue(entered.wait(1))
        self.assertFalse(release.is_set())
        release.set()
        controller.stop()
        self.assertTrue(returned_quickly)


if __name__ == "__main__":
    unittest.main()
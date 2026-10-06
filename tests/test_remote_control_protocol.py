import unittest

from server.config import Settings
from server.services.remote_control import (
    valid_control_input,
    valid_control_start_ack,
    valid_control_stop_ack,
    valid_screen_start_ack,
    valid_screen_stop_ack,
    valid_viewer_message,
)


class RemoteControlProtocolTests(unittest.TestCase):
    def test_viewer_messages_are_allowlisted_and_exact(self):
        self.assertTrue(valid_viewer_message({"type": "control.start"}))
        self.assertTrue(
            valid_viewer_message(
                {"type": "control.input", "action": "key.down", "key": "ctrl"}
            )
        )
        self.assertFalse(valid_viewer_message({"type": "control.start", "command": "run"}))
        self.assertFalse(valid_viewer_message({"type": "command", "command": "run"}))

    def test_control_input_bounds_coordinates_scroll_and_keys(self):
        message = {
            "type": "control.input",
            "action": "pointer.scroll",
            "x": 0.5,
            "y": 1,
            "delta_y": -1200,
        }
        self.assertTrue(valid_control_input(message))
        self.assertFalse(valid_control_input({**message, "delta_y": 1201}))
        self.assertFalse(valid_control_input({**message, "x": True}))
        self.assertFalse(valid_control_input({**message, "action": []}))
        self.assertFalse(
            valid_control_input(
                {"type": "control.input", "action": "pointer.down", "x": 0, "y": 0, "button": []}
            )
        )
        self.assertFalse(
            valid_control_input({"type": "control.input", "action": "key.down", "key": "f13"})
        )

    def test_agent_acknowledgements_validate_resolution_and_state(self):
        settings = Settings(
            database_url="sqlite://",
            agent_token="agent",
            dashboard_token="dashboard",
            screen_max_width=640,
            screen_max_height=360,
        )
        screen_ack = {
            "type": "screen.start_ack",
            "accepted": True,
            "width": 640,
            "height": 360,
            "screen_width": 1920,
            "screen_height": 1080,
            "max_fps": 3,
        }
        self.assertTrue(valid_screen_start_ack(screen_ack, settings))
        self.assertFalse(valid_screen_start_ack({**screen_ack, "width": 641}, settings))
        self.assertTrue(
            valid_control_start_ack(
                {
                    "type": "control.start_ack",
                    "accepted": True,
                    "screen_width": 1920,
                    "screen_height": 1080,
                }
            )
        )
        self.assertTrue(
            valid_control_stop_ack({"type": "control.stop_ack", "stopped": True})
        )
        self.assertTrue(
            valid_screen_stop_ack({"type": "screen.stop_ack", "stopped": True})
        )
        self.assertFalse(
            valid_screen_stop_ack(
                {"type": "screen.stop_ack", "stopped": True, "reason": []}
            )
        )


if __name__ == "__main__":
    unittest.main()
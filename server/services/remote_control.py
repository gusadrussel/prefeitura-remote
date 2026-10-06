"""Strict schemas for the bounded remote-input WebSocket protocol."""

import math

CONTROL_KEYS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789") | frozenset(
    "enter esc tab space backspace delete insert home end pageup pagedown "
    "up down left right ctrl alt shift win f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12 "
    "- = [ ] \\ ; ' , . / `".split()
)
CONTROL_REASONS = frozenset(
    {
        "already_running",
        "screen_not_running",
        "input_unavailable",
        "screen_stopped",
        "session_inactive",
        "input_failed",
        "authorization_revoked",
        "agent_disconnected",
        "not_authorized",
        "agent_busy",
        "capture_failed",
        "channel_unavailable",
    }
)


def _coordinate(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def valid_control_input(message: dict) -> bool:
    if not isinstance(message, dict) or message.get("type") != "control.input":
        return False
    action = message.get("action")
    if not isinstance(action, str):
        return False
    fields = {
        "pointer.move": {"type", "action", "x", "y"},
        "pointer.down": {"type", "action", "x", "y", "button"},
        "pointer.up": {"type", "action", "x", "y", "button"},
        "pointer.scroll": {"type", "action", "x", "y", "delta_y"},
    }
    if action in fields:
        if set(message) != fields[action] or not _coordinate(message["x"]) or not _coordinate(message["y"]):
            return False
        if action in {"pointer.down", "pointer.up"}:
            return isinstance(message["button"], str) and message["button"] in {"left", "right"}
        if action == "pointer.scroll":
            delta = message["delta_y"]
            return (
                isinstance(delta, (int, float))
                and not isinstance(delta, bool)
                and math.isfinite(delta)
                and -1200 <= delta <= 1200
            )
        return True
    if action in {"key.down", "key.up"}:
        return (
            set(message) == {"type", "action", "key"}
            and isinstance(message["key"], str)
            and message["key"] in CONTROL_KEYS
        )
    return False


def valid_viewer_message(message: dict) -> bool:
    if not isinstance(message, dict):
        return False
    message_type = message.get("type")
    if not isinstance(message_type, str):
        return False
    if message_type in {"screen.start", "screen.stop", "control.start", "control.stop"}:
        return set(message) == {"type"}
    if message_type == "control.input":
        return valid_control_input(message)
    return False


def valid_screen_start_ack(message: dict, settings) -> bool:
    if not isinstance(message, dict) or not isinstance(message.get("accepted"), bool):
        return False
    if message["accepted"] is False:
        return (
            set(message) == {"type", "accepted", "reason"}
            and message.get("type") == "screen.start_ack"
            and isinstance(message["reason"], str)
            and len(message["reason"]) <= 64
        )
    expected = {
        "type", "accepted", "width", "height", "screen_width", "screen_height", "max_fps"
    }
    if message.get("type") != "screen.start_ack" or set(message) != expected:
        return False
    bounds = {
        "width": (1, min(settings.screen_max_width, 1280)),
        "height": (1, min(settings.screen_max_height, 720)),
        "screen_width": (1, 32768),
        "screen_height": (1, 32768),
        "max_fps": (1, min(settings.screen_max_fps, 5)),
    }
    return all(
        isinstance(message[key], int)
        and not isinstance(message[key], bool)
        and lower <= message[key] <= upper
        for key, (lower, upper) in bounds.items()
    )


def valid_control_start_ack(message: dict) -> bool:
    if not isinstance(message, dict) or not isinstance(message.get("accepted"), bool):
        return False
    if message["accepted"] is False:
        return (
            set(message) == {"type", "accepted", "reason"}
            and message.get("type") == "control.start_ack"
            and isinstance(message["reason"], str)
            and message["reason"] in CONTROL_REASONS
        )
    return (
        set(message) == {"type", "accepted", "screen_width", "screen_height"}
        and message.get("type") == "control.start_ack"
        and all(
            isinstance(message[key], int)
            and not isinstance(message[key], bool)
            and 1 <= message[key] <= 32768
            for key in ("screen_width", "screen_height")
        )
    )


def valid_control_stop_ack(message: dict) -> bool:
    return (
        isinstance(message, dict)
        and message.get("type") == "control.stop_ack"
        and message.get("stopped") is True
        and set(message).issubset({"type", "stopped", "reason"})
        and (
            "reason" not in message
            or (
                isinstance(message["reason"], str)
                and message["reason"] in CONTROL_REASONS
            )
        )
    )


def valid_screen_stop_ack(message: dict) -> bool:
    return (
        isinstance(message, dict)
        and message.get("type") == "screen.stop_ack"
        and message.get("stopped") is True
        and set(message).issubset({"type", "stopped", "reason"})
        and ("reason" not in message or message["reason"] == "capture_failed")
    )
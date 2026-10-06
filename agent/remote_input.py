"""Explicit, bounded mouse and keyboard control for authorized screen sessions."""

import math
import logging
from queue import Empty, Full, Queue
import sys
from threading import Thread

CONTROL_KEYS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789") | frozenset(
    "enter esc tab space backspace delete insert home end pageup pagedown "
    "up down left right ctrl alt shift win f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12 "
    "- = [ ] \\ ; ' , . / `".split()
)
POINTER_BUTTONS = frozenset({"left", "right"})
MAX_PENDING_INPUTS = 64
_STOP_WORKER = object()
LOGGER = logging.getLogger("prefeitura_remote.agent.control")


def _normalized_coordinate(value) -> bool:
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
    coordinate_actions = {
        "pointer.move": {"type", "action", "x", "y"},
        "pointer.down": {"type", "action", "x", "y", "button"},
        "pointer.up": {"type", "action", "x", "y", "button"},
        "pointer.scroll": {"type", "action", "x", "y", "delta_y"},
    }
    if action in coordinate_actions:
        if set(message) != coordinate_actions[action]:
            return False
        if not _normalized_coordinate(message["x"]) or not _normalized_coordinate(message["y"]):
            return False
        if action in {"pointer.down", "pointer.up"}:
            return isinstance(message["button"], str) and message["button"] in POINTER_BUTTONS
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


class RemoteInputController:
    def __init__(self, backend=None, *, platform_name: str | None = None) -> None:
        if backend is None:
            if (platform_name or sys.platform) != "win32":
                raise RuntimeError("Controle remoto disponível somente no agente Windows")
            import pyautogui

            pyautogui.PAUSE = 0
            backend = pyautogui
        self._backend = backend
        self._width = 0
        self._height = 0
        self._active = False
        self._worker_failed = False
        self._failure_reason: str | None = None
        self._queue: Queue = Queue(maxsize=MAX_PENDING_INPUTS)
        self._worker: Thread | None = None
        self._keys_down: set[str] = set()
        self._buttons_down: set[str] = set()

    def start(self, width: int, height: int) -> None:
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or not 1 <= width <= 32768
            or not 1 <= height <= 32768
        ):
            raise ValueError("Resolução de controle inválida")
        self._width = width
        self._height = height
        self._queue = Queue(maxsize=MAX_PENDING_INPUTS)
        self._worker_failed = False
        self._failure_reason = None
        self._active = True
        self._worker = Thread(target=self._run, name="prefeitura-remote-input", daemon=True)
        self._worker.start()

    def apply(self, message: dict) -> None:
        if not self._active or self._worker_failed or not valid_control_input(message):
            raise ValueError("Mensagem de controle inválida ou sessão inativa")
        try:
            self._queue.put_nowait(dict(message))
        except Full as exc:
            LOGGER.warning("remote_input_queue_full max_pending=%s", MAX_PENDING_INPUTS)
            raise RuntimeError("Fila de entrada remota cheia") from exc

    @property
    def failed(self) -> bool:
        return self._worker_failed

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    def _run(self) -> None:
        try:
            while True:
                message = self._queue.get()
                if message is _STOP_WORKER:
                    break
                self._apply_now(message)
        except Exception as exc:
            self._worker_failed = True
            self._failure_reason = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("remote_input_worker_failed reason=%s", self._failure_reason)
        finally:
            self._release_held_inputs()
            self._discard_pending_inputs()
            self._active = False

    def _apply_now(self, message: dict) -> None:
        action = message["action"]
        if action.startswith("pointer."):
            x = min(self._width - 1, round(message["x"] * (self._width - 1)))
            y = min(self._height - 1, round(message["y"] * (self._height - 1)))
            self._backend.moveTo(x, y)
            if action == "pointer.down" and message["button"] not in self._buttons_down:
                self._backend.mouseDown(button=message["button"])
                self._buttons_down.add(message["button"])
            elif action == "pointer.up" and message["button"] in self._buttons_down:
                self._backend.mouseUp(button=message["button"])
                self._buttons_down.discard(message["button"])
            elif action == "pointer.scroll":
                steps = max(-12, min(12, round(-message["delta_y"] / 100)))
                if steps:
                    self._backend.scroll(steps)
        elif action == "key.down" and message["key"] not in self._keys_down:
            self._backend.keyDown(message["key"])
            self._keys_down.add(message["key"])
        elif action == "key.up" and message["key"] in self._keys_down:
            self._backend.keyUp(message["key"])
            self._keys_down.discard(message["key"])

    def _discard_pending_inputs(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                return

    def _release_held_inputs(self) -> None:
        for key in tuple(self._keys_down):
            try:
                self._backend.keyUp(key)
            except Exception:
                pass
        for button in tuple(self._buttons_down):
            try:
                self._backend.mouseUp(button=button)
            except Exception:
                pass
        self._keys_down.clear()
        self._buttons_down.clear()

    def stop(self) -> None:
        self._active = False
        worker = self._worker
        if worker is None:
            self._release_held_inputs()
            return
        self._discard_pending_inputs()
        try:
            if worker.is_alive():
                self._queue.put(_STOP_WORKER)
        except Exception:
            pass
        worker.join()
        self._worker = None

    close = stop
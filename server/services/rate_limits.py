"""Bounded in-process fixed-window rate limiting."""

import time
from dataclasses import dataclass
from threading import Lock

MAX_TRACKED_RATE_KEYS = 8192


@dataclass
class _RateWindow:
    started_at: float
    attempts: int
    limit: int
    window_seconds: int


class InMemoryRateLimiter:
    def __init__(self, max_keys: int = MAX_TRACKED_RATE_KEYS) -> None:
        self._max_keys = max_keys
        self._windows: dict[str, _RateWindow] = {}
        self._lock = Lock()

    def allow(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
        now: float | None = None,
    ) -> bool:
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate limits and windows must be positive")
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            window = self._windows.get(key)
            if window is not None and (
                timestamp >= window.started_at + window.window_seconds
                or window.limit != limit
                or window.window_seconds != window_seconds
            ):
                del self._windows[key]
                window = None

            if window is None:
                self._purge_expired(timestamp)
                if len(self._windows) >= self._max_keys:
                    return False
                window = _RateWindow(timestamp, 0, limit, window_seconds)
                self._windows[key] = window

            if window.attempts >= limit:
                return False
            window.attempts += 1
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._windows.pop(key, None)

    def is_limited(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
        now: float | None = None,
    ) -> bool:
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate limits and windows must be positive")
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            window = self._windows.get(key)
            if window is None:
                return False
            if (
                timestamp >= window.started_at + window.window_seconds
                or window.limit != limit
                or window.window_seconds != window_seconds
            ):
                del self._windows[key]
                return False
            return window.attempts >= limit

    def _purge_expired(self, now: float) -> None:
        expired = [
            key
            for key, window in self._windows.items()
            if now >= window.started_at + window.window_seconds
        ]
        for key in expired:
            self._windows.pop(key, None)


def peer_address(client) -> str:
    """Return the socket peer only; forwarded headers are intentionally ignored."""
    return client.host if client is not None and client.host else "unknown"

"""Password hashing and expiring in-process technician sessions."""
import hashlib
import hmac
import secrets
import time
from threading import RLock

_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 8 * 60 * 60


class SessionCapacityError(RuntimeError):
    """No space for another live technician session."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2$sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        _, alg, iterations, salt_hex, digest_hex = encoded.split("$")
        if alg != "sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (AttributeError, ValueError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


class TechnicianSessionRegistry:
    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS, max_sessions: int = 4096):
        if ttl_seconds <= 0 or max_sessions <= 0:
            raise ValueError("Session limits must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._sessions: dict[str, tuple[int, float]] = {}
        self._lock = RLock()

    def issue(self, technician_id: int) -> str:
        with self._lock:
            now = time.monotonic()
            self._sessions = {token: entry for token, entry in self._sessions.items() if entry[1] > now}
            if len(self._sessions) >= self._max_sessions:
                raise SessionCapacityError("Limite de sessões ativas atingido")
            token = new_session_token()
            self._sessions[token] = (technician_id, now + self._ttl_seconds)
            return token

    def get(self, token: str | None) -> int | None:
        with self._lock:
            key = token or ""
            session = self._sessions.get(key)
            if not session:
                return None
            technician_id, expires_at = session
            if time.monotonic() >= expires_at:
                self._sessions.pop(key, None)
                return None
            return technician_id

    def revoke(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def revoke_technician(self, technician_id: int) -> None:
        with self._lock:
            for token, (session_technician_id, _expires_at) in list(self._sessions.items()):
                if session_technician_id == technician_id:
                    self._sessions.pop(token, None)

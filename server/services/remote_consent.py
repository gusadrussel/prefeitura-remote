"""Ephemeral, single-use user consent requests for remote support."""

import secrets
import time
from dataclasses import dataclass
from threading import Lock

CONSENT_TTL_SECONDS = 60
MAX_PENDING_CONSENT_REQUESTS = 1024


class ConsentCapacityError(RuntimeError):
    """Raised when the bounded consent store is full."""


@dataclass
class ConsentRequest:
    request_id: str
    agent_id: str
    computer_id: int
    technician_name: str
    expires_at: float
    status: str = "pending"
    ticket: str | None = None
    ticket_expires_in: int | None = None
    reason: str | None = None


class RemoteConsentRegistry:
    def __init__(self) -> None:
        self._requests: dict[str, ConsentRequest] = {}
        self._lock = Lock()

    def create(
        self,
        agent_id: str,
        computer_id: int,
        technician_name: str,
        ttl_seconds: int = CONSENT_TTL_SECONDS,
    ) -> ConsentRequest:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            if len(self._requests) >= MAX_PENDING_CONSENT_REQUESTS:
                raise ConsentCapacityError("Limite de solicitações de consentimento atingido")
            request = ConsentRequest(
                request_id=secrets.token_urlsafe(24),
                agent_id=agent_id,
                computer_id=computer_id,
                technician_name=technician_name,
                expires_at=now + ttl_seconds,
            )
            self._requests[request.request_id] = request
            return request

    def get(self, request_id: str) -> ConsentRequest | None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            request = self._requests.get(request_id)
            if request is None:
                return None
            if request.status == "pending" and request.expires_at <= now:
                request.status = "expired"
                request.reason = "timeout"
            return request

    def respond(self, request_id: str, agent_id: str, computer_id: int, accepted: bool) -> ConsentRequest | None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            request = self._requests.get(request_id)
            if request is None or request.agent_id != agent_id or request.computer_id != computer_id:
                return None
            if request.status != "pending":
                return request
            if request.expires_at <= now:
                request.status = "expired"
                request.reason = "timeout"
                return request
            request.status = "approved" if accepted else "rejected"
            request.reason = "user_accepted" if accepted else "user_rejected"
            return request

    def attach_ticket(self, request_id: str, ticket: str, expires_in: int) -> bool:
        with self._lock:
            request = self._requests.get(request_id)
            if request is None or request.status != "approved":
                return False
            request.ticket = ticket
            request.ticket_expires_in = expires_in
            return True

    def cancel_for_computer(self, computer_id: int, reason: str) -> list[ConsentRequest]:
        with self._lock:
            cancelled = []
            for request in self._requests.values():
                if request.computer_id == computer_id and request.status == "pending":
                    request.status = "cancelled"
                    request.reason = reason
                    cancelled.append(request)
            return cancelled

    def _purge(self, now: float) -> None:
        stale = [
            request_id
            for request_id, request in self._requests.items()
            if request.status in {"expired", "rejected", "cancelled"}
            and request.expires_at + 300 <= now
        ]
        for request_id in stale:
            self._requests.pop(request_id, None)

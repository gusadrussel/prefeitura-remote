"""Ephemeral, single-use user consent requests for remote support."""

import secrets
import time
from dataclasses import dataclass, field
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
    mode: str
    expires_at: float
    status: str = "pending"
    ticket: str | None = None
    ticket_expires_in: int | None = None
    reason: str | None = None
    principal: str = ""
    parent_id: str | None = None
    session_token: str | None = field(default=None, repr=False)
    bootstrap: bool = False


class RemoteConsentRegistry:
    def __init__(self) -> None:
        self._requests: dict[str, ConsentRequest] = {}
        self._lock = Lock()

    def create(
        self,
        agent_id: str,
        computer_id: int,
        technician_name: str,
        mode: str = "view",
        ttl_seconds: int = CONSENT_TTL_SECONDS,
        principal: str = "",
        parent_id: str | None = None,
        session_token: str | None = None,
        bootstrap: bool = False,
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
                mode=mode,
                expires_at=now + ttl_seconds,
                principal=principal, parent_id=parent_id,
                session_token=session_token,
                bootstrap=bootstrap,
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
                return None
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
            if request is None or request.status != "approved" or (request.ticket and request.ticket != ticket):
                return False
            request.ticket = ticket
            request.ticket_expires_in = expires_in
            return True

    def issue_view_ticket(self, request_id: str, streams) -> tuple[str, int] | None:
        """Serialize concurrent polling with cancellation and ticket issuance.

        Lock order is consent -> ticket store; ticket-store methods never acquire
        the consent lock. Each approved request allocates at most one ticket.
        """
        with self._lock:
            self._purge(time.monotonic())
            request = self._requests.get(request_id)
            if request is None or request.status != "approved" or request.mode != "view":
                return None
            if request.ticket is None:
                ticket, expires = streams.issue_ticket(request.agent_id, request.computer_id)
                request.ticket, request.ticket_expires_in = ticket, expires
            return request.ticket, request.ticket_expires_in

    def cancel_for_computer(self, computer_id: int, reason: str) -> list[ConsentRequest]:
        with self._lock:
            cancelled = []
            for request in self._requests.values():
                if request.computer_id == computer_id and request.status in {"pending", "approved", "active"}:
                    request.status = "cancelled"
                    request.reason = reason
                    cancelled.append(request)
            return cancelled

    def for_ticket(self, ticket: str, computer_id: int) -> ConsentRequest | None:
        with self._lock:
            return next((r for r in self._requests.values()
                         if r.ticket == ticket and r.computer_id == computer_id
                         and r.mode == "view" and r.status == "approved"
                         and r.expires_at > time.monotonic()), None)

    def claim(self, request_id: str, mode: str) -> bool:
        with self._lock:
            request = self._requests.get(request_id)
            if (request is None or request.mode != mode or request.status != "approved"
                    or request.expires_at <= time.monotonic()):
                return False
            request.status = "active"
            return True

    def claim_control(self, parent_id: str) -> str | None:
        with self._lock:
            parent = self._requests.get(parent_id)
            if parent is None or parent.status != "active":
                return None
            for request in self._requests.values():
                if (request.parent_id == parent_id and request.mode == "control"
                        and request.status == "approved" and request.principal == parent.principal
                        and request.bootstrap == parent.bootstrap
                        and request.agent_id == parent.agent_id and request.computer_id == parent.computer_id
                        and request.expires_at > time.monotonic()):
                    request.status = "active"
                    return request.request_id
            return None

    def active(self, request_id: str | None, mode: str) -> bool:
        with self._lock:
            request = self._requests.get(request_id)
            return bool(request and request.mode == mode and request.status == "active")

    def end(self, request_id: str | None, reason: str) -> None:
        if request_id is None:
            return
        with self._lock:
            for request in self._requests.values():
                if (request.request_id == request_id or request.parent_id == request_id) and request.status in {"pending", "approved", "active"}:
                    request.status = "cancelled"
                    request.reason = reason

    def cancel_principal(self, principal: str) -> set[str]:
        return self._cancel_matching(lambda request: request.principal == principal, "technician_revoked")

    def cancel_session(self, token: str) -> set[str]:
        return self._cancel_matching(lambda request: request.session_token == token, "technician_logout")

    def _cancel_matching(self, matches, reason: str) -> set[str]:
        with self._lock:
            agents = set()
            for request in self._requests.values():
                if matches(request) and request.status in {"pending", "approved", "active"}:
                    request.status = "cancelled"
                    request.reason = reason
                    agents.add(request.agent_id)
            return agents

    def _purge(self, now: float) -> None:
        for request in self._requests.values():
            if request.status in {"pending", "approved"} and request.expires_at <= now:
                request.status = "expired"
                request.reason = "timeout"
        stale = [
            request_id
            for request_id, request in self._requests.items()
            if request.status in {"expired", "rejected", "cancelled"}
            and request.expires_at + 300 <= now
        ]
        for request_id in stale:
            self._requests.pop(request_id, None)


def technician_session_valid(app, consent: ConsentRequest | None) -> bool:
    if consent is None or consent.status not in {"approved", "active"}:
        return False
    if consent.bootstrap:
        return bool(app.state.settings.dashboard_token)
    return app.state.technician_sessions.get(consent.session_token) is not None

"""Hashing helpers for high-entropy per-agent credentials."""

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any


def token_matches(provided: str, expected: str) -> bool:
    """Constant-time comparison also for malformed/non-ASCII HTTP headers."""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def hash_agent_credential(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def verify_agent_credential(credential: str, stored_hash: str | None) -> bool:
    if not credential or not stored_hash:
        return False
    return token_matches(hash_agent_credential(credential), stored_hash)


def has_active_agent_credential(computer: Any, settings: Any) -> bool:
    if computer.agent_credential_revoked:
        return False
    expires_at = computer.agent_credential_expires_at
    # SQLite strips timezone metadata. Stored naive timestamps are UTC, never
    # the server's local timezone (e.g. America/Sao_Paulo).
    if expires_at is not None:
        expires_at = expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at.astimezone(UTC)
    if expires_at is not None and expires_at <= datetime.now(UTC):
        return False
    if computer.agent_credential_hash is not None:
        return True
    return bool(settings.allow_legacy_agent_token and settings.agent_token)

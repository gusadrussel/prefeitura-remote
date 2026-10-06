"""Hashing helpers for high-entropy per-agent credentials."""

import hashlib
import hmac
from typing import Any


def hash_agent_credential(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def verify_agent_credential(credential: str, stored_hash: str | None) -> bool:
    if not credential or not stored_hash:
        return False
    return hmac.compare_digest(hash_agent_credential(credential), stored_hash)


def has_active_agent_credential(computer: Any, settings: Any) -> bool:
    if computer.agent_credential_revoked:
        return False
    if computer.agent_credential_hash is not None:
        return True
    return bool(settings.allow_legacy_agent_token and settings.agent_token)

"""Bounded JSON audit log with an explicit field allowlist (never request bodies)."""
from datetime import UTC, datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from threading import Event
import stat

LOGGER = logging.getLogger("prefeitura_remote.audit")
FIELDS = frozenset({"principal", "computer_id", "agent_id", "request_id", "session_id",
                    "mode", "reason", "outcome", "peer", "technician_id", "principal_type"})


class ObservableAuditHandler(RotatingFileHandler):
    """Latch write/rotation failure for readiness without interrupting stops."""
    def __init__(self, *args, **kwargs):
        self.failed = Event()
        super().__init__(*args, **kwargs)

    def handleError(self, record):
        # logging's default handler may print record data and silently continue.
        # A failed audit stays unhealthy until the configured handler is reopened.
        self.failed.set()


class AuditFormatter(logging.Formatter):
    def format(self, record):
        data = {"timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "event": getattr(record, "audit_event", record.getMessage().split(" ",1)[0]),
                "level": record.levelname}
        for key in FIELDS:
            value = getattr(record, "audit_fields", {}).get(key)
            if value is not None:
                data[key] = value if isinstance(value, (int, bool)) else str(value)[:256]
        # Legacy control lifecycle records contain only principal/IDs/reason,
        # but are formatted as text until their call sites are converted.
        if not hasattr(record, "audit_event"):
            data["message"] = record.getMessage()[:2000]
        return json.dumps(data, ensure_ascii=True)


def audit_event(event: str, **fields):
    LOGGER.info(event, extra={"audit_event":event, "audit_fields":{k:v for k,v in fields.items() if k in FIELDS}})


def audit_available(app):
    if not getattr(app.state.settings, 'audit_log', ''):
        return True
    handler = getattr(app.state, 'audit_handler', None)
    return handler is not None and not handler.failed.is_set()


def configure_audit(filename: str):
    if not filename:
        return None
    path = Path(filename)
    if path.is_symlink():
        raise ValueError("Audit log must not be a symlink")
    # Parent must already be provisioned for the service account; fail startup
    # instead of silently disabling audit or creating broad-permission folders.
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os,'O_NOFOLLOW',0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('Audit destination must be a regular file')
    finally:
        os.close(descriptor)
    handler = ObservableAuditHandler(path, maxBytes=10*1024*1024, backupCount=4, encoding="utf-8")
    handler.setFormatter(AuditFormatter())
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    return handler


def close_audit(handler):
    if handler is not None:
        LOGGER.removeHandler(handler)
        handler.close()

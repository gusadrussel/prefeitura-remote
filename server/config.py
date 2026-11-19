"""Server settings loaded from environment variables."""

import os
import math
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = f"sqlite:///{PROJECT_ROOT / 'prefeitura_remote.db'}"


@dataclass(frozen=True)
class Settings:
    database_url: str
    agent_token: str
    dashboard_token: str
    enrollment_token: str = ""
    allow_legacy_agent_token: bool = False
    online_threshold_seconds: int = 900
    agent_heartbeat_interval_seconds: float = 30
    agent_heartbeat_timeout_seconds: float = 90
    agent_hello_timeout_seconds: float = 10
    screen_max_fps: int = 5
    screen_max_width: int = 1280
    screen_max_height: int = 720
    screen_jpeg_quality: int = 55
    screen_max_frame_bytes: int = 750_000
    rate_limit_window_seconds: int = 60
    auth_failures_per_window: int = 10
    inventory_updates_per_window: int = 30
    viewer_ticket_requests_per_window: int = 20
    authorization_changes_per_window: int = 20
    websocket_connections_per_window: int = 60
    dashboard_operator_name: str = "Técnico da TI"
    remote_consent_ttl_seconds: int = 60
    remote_consent_requests_per_window: int = 10
    agent_credential_ttl_days: int = 90
    hsts_enabled: bool = True
    auto_migrate: bool = True
    database_pool_size: int = 3
    database_max_overflow: int = 2
    database_pool_timeout: float = 5
    audit_log: str = ""
    max_screen_viewers: int = 8
    max_request_bytes: int = 1_048_576
    sqlite_journal_mode: str = ''
    

    def __post_init__(self) -> None:
        if self.sqlite_journal_mode not in {'','DELETE','PERSIST'}:
            raise ValueError('Unsupported SQLite journal mode')
        if any(token.startswith("REPLACE_") for token in (self.agent_token,self.dashboard_token,self.enrollment_token)):
            raise ValueError("Substitua os segredos de exemplo antes de iniciar o serviço")
        limits = (
            self.rate_limit_window_seconds,
            self.auth_failures_per_window,
            self.inventory_updates_per_window,
            self.viewer_ticket_requests_per_window,
            self.authorization_changes_per_window,
            self.websocket_connections_per_window,
            self.remote_consent_ttl_seconds,
            self.remote_consent_requests_per_window,
            self.agent_credential_ttl_days,
            self.database_pool_size,
            self.max_screen_viewers,
            self.max_request_bytes,
        )
        if any(value < 1 for value in limits):
            raise ValueError("Rate limits and windows must be positive integers")
        if self.database_max_overflow < 0 or not math.isfinite(self.database_pool_timeout) or self.database_pool_timeout <= 0:
            raise ValueError("Database pool limits must be bounded and positive")
        for value in (self.agent_heartbeat_interval_seconds,self.agent_heartbeat_timeout_seconds,self.agent_hello_timeout_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Heartbeat and handshake deadlines must be finite and positive")


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("PREFEITURA_DATABASE_URL", DEFAULT_DATABASE_URL),
        agent_token=os.getenv("PREFEITURA_AGENT_TOKEN", ""),
        dashboard_token=os.getenv("PREFEITURA_DASHBOARD_TOKEN", ""),
        enrollment_token=os.getenv("PREFEITURA_ENROLLMENT_TOKEN", ""),
        allow_legacy_agent_token=os.getenv(
            "PREFEITURA_ALLOW_LEGACY_AGENT_TOKEN", "false"
        ).strip().lower() in {"1", "true", "yes", "on"},
        online_threshold_seconds=int(os.getenv("PREFEITURA_ONLINE_THRESHOLD", "900")),
        agent_heartbeat_interval_seconds=float(
            os.getenv("PREFEITURA_AGENT_HEARTBEAT_INTERVAL", "30")
        ),
        agent_heartbeat_timeout_seconds=float(
            os.getenv("PREFEITURA_AGENT_HEARTBEAT_TIMEOUT", "90")
        ),
        agent_hello_timeout_seconds=float(os.getenv("PREFEITURA_AGENT_HELLO_TIMEOUT", "10")),
        screen_max_fps=int(os.getenv("PREFEITURA_SCREEN_MAX_FPS", "5")),
        screen_max_width=int(os.getenv("PREFEITURA_SCREEN_MAX_WIDTH", "1280")),
        screen_max_height=int(os.getenv("PREFEITURA_SCREEN_MAX_HEIGHT", "720")),
        screen_jpeg_quality=int(os.getenv("PREFEITURA_SCREEN_JPEG_QUALITY", "55")),
        screen_max_frame_bytes=int(os.getenv("PREFEITURA_SCREEN_MAX_FRAME_BYTES", "750000")),
        rate_limit_window_seconds=int(os.getenv("PREFEITURA_RATE_LIMIT_WINDOW_SECONDS", "60")),
        auth_failures_per_window=int(os.getenv("PREFEITURA_AUTH_FAILURE_LIMIT", "10")),
        inventory_updates_per_window=int(os.getenv("PREFEITURA_INVENTORY_UPDATE_LIMIT", "30")),
        viewer_ticket_requests_per_window=int(
            os.getenv("PREFEITURA_VIEWER_TICKET_LIMIT", "20")
        ),
        authorization_changes_per_window=int(
            os.getenv("PREFEITURA_AUTHORIZATION_CHANGE_LIMIT", "20")
        ),
        websocket_connections_per_window=int(
            os.getenv("PREFEITURA_WEBSOCKET_CONNECTION_LIMIT", "60")
        ),
        dashboard_operator_name=os.getenv("PREFEITURA_DASHBOARD_OPERATOR_NAME", "Técnico da TI").strip() or "Técnico da TI",
        remote_consent_ttl_seconds=int(os.getenv("PREFEITURA_REMOTE_CONSENT_TTL", "60")),
        remote_consent_requests_per_window=int(os.getenv("PREFEITURA_REMOTE_CONSENT_LIMIT", "10")),
        agent_credential_ttl_days=int(os.getenv("PREFEITURA_AGENT_CREDENTIAL_TTL_DAYS", "90")),
        hsts_enabled=os.getenv("PREFEITURA_HSTS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        auto_migrate=os.getenv("PREFEITURA_AUTO_MIGRATE", "true").strip().lower() in {"1","true","yes","on"},
        database_pool_size=int(os.getenv("PREFEITURA_DATABASE_POOL_SIZE", "3")),
        database_max_overflow=int(os.getenv("PREFEITURA_DATABASE_MAX_OVERFLOW", "2")),
        database_pool_timeout=float(os.getenv("PREFEITURA_DATABASE_POOL_TIMEOUT", "5")),
        audit_log=os.getenv("PREFEITURA_AUDIT_LOG", ""),
        max_screen_viewers=int(os.getenv("PREFEITURA_MAX_SCREEN_VIEWERS", "8")),
        max_request_bytes=int(os.getenv("PREFEITURA_MAX_REQUEST_BYTES", "1048576")),
        sqlite_journal_mode=os.getenv('PREFEITURA_SQLITE_JOURNAL_MODE','').strip().upper(),
    )

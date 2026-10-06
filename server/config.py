"""Server settings loaded from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = f"sqlite:///{PROJECT_ROOT / 'prefeitura_remote.db'}"


@dataclass(frozen=True)
class Settings:
    database_url: str
    agent_token: str
    dashboard_token: str
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

    def __post_init__(self) -> None:
        limits = (
            self.rate_limit_window_seconds,
            self.auth_failures_per_window,
            self.inventory_updates_per_window,
            self.viewer_ticket_requests_per_window,
            self.authorization_changes_per_window,
            self.websocket_connections_per_window,
            self.remote_consent_ttl_seconds,
            self.remote_consent_requests_per_window,
        )
        if any(value < 1 for value in limits):
            raise ValueError("Rate limits and windows must be positive integers")


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("PREFEITURA_DATABASE_URL", DEFAULT_DATABASE_URL),
        agent_token=os.getenv("PREFEITURA_AGENT_TOKEN", ""),
        dashboard_token=os.getenv("PREFEITURA_DASHBOARD_TOKEN", ""),
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
    )
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
    online_threshold_seconds: int = 900
    agent_heartbeat_interval_seconds: float = 30
    agent_heartbeat_timeout_seconds: float = 90
    agent_hello_timeout_seconds: float = 10
    screen_max_fps: int = 5
    screen_max_width: int = 1280
    screen_max_height: int = 720
    screen_jpeg_quality: int = 55
    screen_max_frame_bytes: int = 750_000


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("PREFEITURA_DATABASE_URL", DEFAULT_DATABASE_URL),
        agent_token=os.getenv("PREFEITURA_AGENT_TOKEN", ""),
        dashboard_token=os.getenv("PREFEITURA_DASHBOARD_TOKEN", ""),
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
    )
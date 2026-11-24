"""FastAPI application factory and local development entry point."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from server.config import Settings, load_settings
from server.database.models import Computer
from server.database.session import create_database
from server.routes.api import router
from server.routes.screen import screen_router
from server.routes.websocket import websocket_router
from server.services.agent_connections import AgentConnectionRegistry
from server.services.rate_limits import InMemoryRateLimiter
from server.services.remote_consent import RemoteConsentRegistry
from server.services.screen_streams import ScreenStreamRegistry
from server.services.technician_auth import TechnicianSessionRegistry
from server.services.audit import configure_audit, close_audit
from server.services.body_limit import BodyLimitMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_app(settings: Settings | None = None, database_url: str | None = None,
               *, initialize_database: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        audit_handler = None
        try:
            audit_handler = configure_audit(app.state.settings.audit_log)
            app.state.audit_handler = audit_handler
            if not hasattr(app.state, "engine"):
                initialize_persistence()
            yield
        finally:
            if hasattr(app.state, "engine"):
                app.state.engine.dispose()
            close_audit(audit_handler)

    app = FastAPI(title="Vyntra Console", version="1.5.0", lifespan=lifespan)
    app.state.settings = settings or load_settings()
    effective_database_url = database_url or app.state.settings.database_url
    def initialize_persistence():
        engine, session_factory = create_database(effective_database_url,
            migrate=app.state.settings.auto_migrate, pool_size=app.state.settings.database_pool_size,
            max_overflow=app.state.settings.database_max_overflow,
            pool_timeout=app.state.settings.database_pool_timeout,
            sqlite_journal_mode=app.state.settings.sqlite_journal_mode)
        app.state.engine = engine
        app.state.session_factory = session_factory

    if initialize_database:
        initialize_persistence()
    app.state.computer_model = Computer
    app.state.agent_connections = AgentConnectionRegistry()
    app.state.screen_streams = ScreenStreamRegistry(max_viewers=app.state.settings.max_screen_viewers)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.remote_consent = RemoteConsentRegistry()
    app.state.technician_sessions = TechnicianSessionRegistry()

    app.add_middleware(BodyLimitMiddleware,maximum=app.state.settings.max_request_bytes)
    if app.state.settings.hsts_enabled:
        class SecurityHeadersMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                response = await call_next(request)
                response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                response.headers.setdefault("X-Content-Type-Options", "nosniff")
                response.headers.setdefault("Referrer-Policy", "same-origin")
                return response
        app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(router)
    app.include_router(websocket_router)
    app.include_router(screen_router)
    app.mount(
        "/dashboard",
        StaticFiles(directory=PROJECT_ROOT / "dashboard", check_dir=False),
        name="dashboard-assets",
    )

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, exc: SQLAlchemyError):
        logging.getLogger("prefeitura_remote.server").exception(
            "Erro não tratado no banco", exc_info=exc
        )
        return JSONResponse(status_code=503, content={"detail": "Banco de dados indisponível"})

    return app


# Importing the ASGI entry point must not open or migrate a database. Uvicorn's
# lifespan initializes persistence; explicit factories retain test compatibility.
app = create_app(initialize_database=False)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.main:app", host="127.0.0.1", port=8000, reload=False)

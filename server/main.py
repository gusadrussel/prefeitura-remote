"""FastAPI application factory and local development entry point."""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.staticfiles import StaticFiles

from server.config import Settings, load_settings
from server.database.models import Computer
from server.database.session import create_database
from server.routes.api import router
from server.routes.screen import screen_router
from server.routes.websocket import websocket_router
from server.services.agent_connections import AgentConnectionRegistry
from server.services.rate_limits import InMemoryRateLimiter
from server.services.screen_streams import ScreenStreamRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_app(settings: Settings | None = None, database_url: str | None = None) -> FastAPI:
    app = FastAPI(title="Prefeitura Remote Inventory", version="0.1.0")
    app.state.settings = settings or load_settings()
    effective_database_url = database_url or app.state.settings.database_url
    engine, session_factory = create_database(effective_database_url)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.computer_model = Computer
    app.state.agent_connections = AgentConnectionRegistry()
    app.state.screen_streams = ScreenStreamRegistry()
    app.state.rate_limiter = InMemoryRateLimiter()
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


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.main:app", host="127.0.0.1", port=8000, reload=False)
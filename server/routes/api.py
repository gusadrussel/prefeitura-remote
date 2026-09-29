"""Authenticated inventory ingestion and technician endpoints."""

import hmac
import logging
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from server.schemas import AuthorizationStatus, ComputerSummary, InventoryAccepted, InventoryPayload
from server.services.inventory import (
    as_utc,
    computer_summary,
    set_authorization_status,
    upsert_inventory,
)

LOGGER = logging.getLogger("prefeitura_remote.server")
router = APIRouter()


def authorize(expected_token: str, label: str) -> Callable:
    def check_token(authorization: str | None = Header(default=None)) -> None:
        if not expected_token:
            raise HTTPException(status_code=503, detail=f"Autenticação {label} não configurada")
        provided = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if not hmac.compare_digest(provided, expected_token):
            raise HTTPException(status_code=401, detail="Credencial inválida")

    return check_token


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def agent_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    authorize(request.app.state.settings.agent_token, "do agente")(authorization)


def dashboard_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    authorize(request.app.state.settings.dashboard_token, "do painel")(authorization)


@router.post("/api/v1/inventory", response_model=InventoryAccepted, status_code=201)
def receive_inventory(
    payload: InventoryPayload,
    request: Request,
    _authorized: None = Depends(agent_auth),
    session: Session = Depends(get_session),
):
    inventory = payload.model_dump()
    try:
        computer = upsert_inventory(session, inventory)
    except SQLAlchemyError:
        session.rollback()
        LOGGER.exception("Falha ao persistir inventário do agente %s", payload.agent_id)
        raise HTTPException(status_code=503, detail="Não foi possível salvar o inventário") from None
    LOGGER.info("Inventário atualizado: %s (%s)", computer.hostname, computer.agent_id)
    return {
        "computer_id": computer.id,
        "agent_id": computer.agent_id,
        "last_seen": as_utc(computer.last_seen),
        "authorization_status": computer.authorization_status,
    }


@router.get("/api/v1/admin/computers", response_model=list[ComputerSummary])
@router.get("/api/v1/computers", response_model=list[ComputerSummary])
def list_computers(
    request: Request,
    search: str = Query(default="", max_length=255),
    authorization_status: AuthorizationStatus | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    _authorized: None = Depends(dashboard_auth),
    session: Session = Depends(get_session),
):
    statement = select(request.app.state.computer_model).order_by(
        request.app.state.computer_model.hostname
    )
    if search.strip():
        statement = statement.where(request.app.state.computer_model.hostname.ilike(f"%{search.strip()}%"))
    if authorization_status:
        statement = statement.where(
            request.app.state.computer_model.authorization_status == authorization_status
        )
    computers = session.scalars(statement.limit(limit)).all()
    threshold = request.app.state.settings.online_threshold_seconds
    connections = request.app.state.agent_connections
    return [
        computer_summary(computer, threshold, connections.get(computer.agent_id))
        for computer in computers
    ]


@router.get("/api/v1/admin/computers/{computer_id}")
@router.get("/api/v1/computers/{computer_id}")
def computer_detail(
    computer_id: int,
    request: Request,
    _authorized: None = Depends(dashboard_auth),
    session: Session = Depends(get_session),
):
    computer = session.get(request.app.state.computer_model, computer_id)
    if computer is None:
        raise HTTPException(status_code=404, detail="Computador não encontrado")
    return {
        "computer": computer_summary(
            computer,
            request.app.state.settings.online_threshold_seconds,
            request.app.state.agent_connections.get(computer.agent_id),
        ),
        "inventory": computer.inventory,
    }


@router.post("/api/v1/admin/computers/{computer_id}/authorize", response_model=ComputerSummary)
def authorize_computer(
    computer_id: int,
    request: Request,
    _authorized: None = Depends(dashboard_auth),
    session: Session = Depends(get_session),
):
    return _update_authorization(session, computer_id, "authorized", request)


@router.post("/api/v1/admin/computers/{computer_id}/revoke", response_model=ComputerSummary)
def revoke_computer(
    computer_id: int,
    request: Request,
    _authorized: None = Depends(dashboard_auth),
    session: Session = Depends(get_session),
):
    return _update_authorization(session, computer_id, "revoked", request)


@router.post("/api/v1/admin/computers/{computer_id}/screen/viewer-ticket")
def create_screen_viewer_ticket(
    computer_id: int,
    request: Request,
    _authorized: None = Depends(dashboard_auth),
    session: Session = Depends(get_session),
):
    computer = session.get(request.app.state.computer_model, computer_id)
    if computer is None:
        raise HTTPException(status_code=404, detail="Computador não encontrado")
    if computer.authorization_status != "authorized":
        raise HTTPException(status_code=403, detail="Computador não autorizado")
    connection = request.app.state.agent_connections.get(computer.agent_id)
    if not connection or not connection.connected or connection.computer_id != computer.id:
        raise HTTPException(status_code=409, detail="Agente não conectado ao canal")
    ticket, expires_in = request.app.state.screen_streams.issue_ticket(
        computer.agent_id, computer.id
    )
    return {"ticket": ticket, "expires_in": expires_in}


def _update_authorization(
    session: Session,
    computer_id: int,
    status: AuthorizationStatus,
    request: Request,
) -> dict:
    try:
        computer = set_authorization_status(session, computer_id, status)
    except SQLAlchemyError:
        session.rollback()
        LOGGER.exception("Falha ao atualizar autorização do computador %s", computer_id)
        raise HTTPException(status_code=503, detail="Não foi possível atualizar a autorização") from None
    if computer is None:
        raise HTTPException(status_code=404, detail="Computador não encontrado")
    LOGGER.info(
        "Autorização do computador %s alterada para %s",
        computer.agent_id,
        computer.authorization_status,
    )
    return computer_summary(
        computer,
        request.app.state.settings.online_threshold_seconds,
        request.app.state.agent_connections.get(computer.agent_id),
    )


@router.get("/api/v1/health")
def health():
    return {"status": "ok"}


@router.get("/")
def dashboard():
    return FileResponse(Path(__file__).resolve().parents[2] / "dashboard" / "index.html")
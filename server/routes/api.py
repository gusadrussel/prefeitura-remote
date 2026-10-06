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
from server.services.rate_limits import peer_address
from server.services.agent_credentials import has_active_agent_credential, verify_agent_credential
from server.services.screen_streams import ScreenTicketCapacityError
from server.services.remote_consent import ConsentCapacityError

LOGGER = logging.getLogger("prefeitura_remote.server")
router = APIRouter()


def authorize(expected_token: str, label: str) -> Callable:
    def check_token(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        if not expected_token:
            raise HTTPException(status_code=503, detail=f"Autenticação {label} não configurada")
        provided = authorization.removeprefix("Bearer ").strip() if authorization else ""
        key = f"auth-failure:{label}:{peer_address(request.client)}"
        if hmac.compare_digest(provided, expected_token):
            request.app.state.rate_limiter.reset(key)
            return
        settings = request.app.state.settings
        if not request.app.state.rate_limiter.allow(
            key,
            limit=settings.auth_failures_per_window,
            window_seconds=settings.rate_limit_window_seconds,
        ):
            raise HTTPException(
                status_code=429,
                detail="Muitas falhas de autenticação; tente novamente mais tarde",
                headers={"Retry-After": str(settings.rate_limit_window_seconds)},
            )
        raise HTTPException(status_code=401, detail="Credencial inválida")

    return check_token


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def dashboard_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    authorize(request.app.state.settings.dashboard_token, "do painel")(request, authorization)


@router.post("/api/v1/inventory", response_model=InventoryAccepted, status_code=201)
def receive_inventory(
    payload: InventoryPayload,
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    settings = request.app.state.settings
    auth_key = f"auth-failure:do agente:{peer_address(request.client)}"
    scheme, separator, provided = (authorization or "").partition(" ")
    provided = provided.strip() if separator and scheme.lower() == "bearer" else ""
    computer = session.scalar(
        select(request.app.state.computer_model).where(
            request.app.state.computer_model.agent_id == payload.agent_id
        )
    )
    has_individual_credential = bool(
        computer
        and (computer.agent_credential_hash is not None or computer.agent_credential_revoked)
    )
    individual_valid = bool(
        computer
        and not computer.agent_credential_revoked
        and verify_agent_credential(provided, computer.agent_credential_hash)
    )
    legacy_valid = bool(
        not has_individual_credential
        and settings.allow_legacy_agent_token
        and settings.agent_token
        and hmac.compare_digest(provided, settings.agent_token)
    )
    if individual_valid or legacy_valid:
        request.app.state.rate_limiter.reset(auth_key)
    if not individual_valid and not legacy_valid:
        if (
            not has_individual_credential
            and not settings.agent_token
            and not settings.allow_legacy_agent_token
        ):
            raise HTTPException(status_code=503, detail="Autenticação do agente não configurada")
        if not request.app.state.rate_limiter.allow(
            auth_key,
            limit=settings.auth_failures_per_window,
            window_seconds=settings.rate_limit_window_seconds,
        ):
            raise HTTPException(
                status_code=429,
                detail="Muitas falhas de autenticação; tente novamente mais tarde",
                headers={"Retry-After": str(settings.rate_limit_window_seconds)},
            )
        raise HTTPException(status_code=401, detail="Credencial inválida")

    rate_key = f"inventory:{peer_address(request.client)}:{payload.agent_id}"
    if not request.app.state.rate_limiter.allow(
        rate_key,
        limit=settings.inventory_updates_per_window,
        window_seconds=settings.rate_limit_window_seconds,
    ):
        raise HTTPException(
            status_code=429,
            detail="Muitas atualizações de inventário para este agente",
            headers={"Retry-After": str(settings.rate_limit_window_seconds)},
        )
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


@router.post("/api/v1/admin/computers/{computer_id}/screen/access-request", status_code=202)
def create_remote_access_request(
    computer_id: int,
    request: Request,
    _authorized: None = Depends(dashboard_auth),
    session: Session = Depends(get_session),
):
    computer = session.get(request.app.state.computer_model, computer_id)
    if computer is None:
        raise HTTPException(status_code=404, detail="Computador não encontrado")
    settings = request.app.state.settings
    if (
        computer.authorization_status != "authorized"
        or not has_active_agent_credential(computer, settings)
    ):
        raise HTTPException(status_code=403, detail="Computador não autorizado")
    connection = request.app.state.agent_connections.get(computer.agent_id)
    if not connection or not connection.connected or connection.computer_id != computer.id:
        raise HTTPException(status_code=409, detail="Agente não conectado ao canal")
    rate_key = f"remote-consent:{peer_address(request.client)}:{computer.id}"
    if not request.app.state.rate_limiter.allow(
        rate_key,
        limit=settings.remote_consent_requests_per_window,
        window_seconds=settings.rate_limit_window_seconds,
    ):
        raise HTTPException(
            status_code=429,
            detail="Muitas solicitações de acesso remoto para este computador",
            headers={"Retry-After": str(settings.rate_limit_window_seconds)},
        )
    try:
        consent = request.app.state.remote_consent.create(
            computer.agent_id,
            computer.id,
            settings.dashboard_operator_name,
            ttl_seconds=settings.remote_consent_ttl_seconds,
        )
    except ConsentCapacityError:
        raise HTTPException(
            status_code=429,
            detail="Limite de solicitações de acesso remoto atingido; tente novamente mais tarde",
        ) from None
    if not request.app.state.agent_connections.send(
        computer.agent_id,
        {
            "type": "session.consent_request",
            "request_id": consent.request_id,
            "technician_name": consent.technician_name,
            "hostname": computer.hostname,
            "expires_in": settings.remote_consent_ttl_seconds,
        },
    ):
        request.app.state.remote_consent.cancel_for_computer(computer.id, "agent_unavailable")
        raise HTTPException(status_code=409, detail="Não foi possível solicitar confirmação no computador")
    LOGGER.info(
        "remote_access_consent_requested technician=%s computer_id=%s agent_id=%s request_id=%s",
        consent.technician_name, computer.id, computer.agent_id, consent.request_id,
    )
    return {
        "request_id": consent.request_id,
        "status": consent.status,
        "technician_name": consent.technician_name,
        "expires_in": settings.remote_consent_ttl_seconds,
    }


@router.post("/api/v1/admin/computers/{computer_id}/screen/access-request/{request_id}/cancel")
def cancel_remote_access_request(
    computer_id: int,
    request_id: str,
    request: Request,
    _authorized: None = Depends(dashboard_auth),
):
    consent = request.app.state.remote_consent.get(request_id)
    if consent is None or consent.computer_id != computer_id:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    request.app.state.remote_consent.cancel_for_computer(computer_id, "technician_cancelled")
    request.app.state.agent_connections.send_urgent(
        consent.agent_id,
        {"type": "session.remote_stop", "reason": "technician_cancelled"},
    )
    return {"status": "cancelled"}


@router.get("/api/v1/admin/computers/{computer_id}/screen/access-request/{request_id}")
def get_remote_access_request(
    computer_id: int,
    request_id: str,
    request: Request,
    _authorized: None = Depends(dashboard_auth),
    session: Session = Depends(get_session),
):
    consent = request.app.state.remote_consent.get(request_id)
    if consent is None or consent.computer_id != computer_id:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    computer = session.get(request.app.state.computer_model, computer_id)
    if computer is None:
        raise HTTPException(status_code=404, detail="Computador não encontrado")
    if consent.status == "approved" and consent.ticket is None:
        if (
            computer.authorization_status != "authorized"
            or not has_active_agent_credential(computer, request.app.state.settings)
            or not (connection := request.app.state.agent_connections.get(computer.agent_id))
            or not connection.connected
        ):
            consent.status = "cancelled"
            consent.reason = "authorization_or_channel_changed"
        else:
            try:
                ticket, expires_in = request.app.state.screen_streams.issue_ticket(
                    computer.agent_id, computer.id
                )
            except ScreenTicketCapacityError:
                return {"status": "approved", "technician_name": consent.technician_name}
            request.app.state.remote_consent.attach_ticket(request_id, ticket, expires_in)
            LOGGER.info(
                "remote_access_consent_approved technician=%s computer_id=%s agent_id=%s request_id=%s",
                consent.technician_name, computer.id, computer.agent_id, request_id,
            )
    response = {
        "status": consent.status,
        "technician_name": consent.technician_name,
        "reason": consent.reason,
    }
    if consent.ticket is not None:
        response.update({"ticket": consent.ticket, "expires_in": consent.ticket_expires_in})
    return response


def _update_authorization(
    session: Session,
    computer_id: int,
    status: AuthorizationStatus,
    request: Request,
) -> dict:
    settings = request.app.state.settings
    rate_key = f"authorization-change:{peer_address(request.client)}:{computer_id}"
    if not request.app.state.rate_limiter.allow(
        rate_key,
        limit=settings.authorization_changes_per_window,
        window_seconds=settings.rate_limit_window_seconds,
    ):
        raise HTTPException(
            status_code=429,
            detail="Muitas alterações de autorização para este computador",
            headers={"Retry-After": str(settings.rate_limit_window_seconds)},
        )
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
    if computer.authorization_status != "authorized":
        request.app.state.remote_consent.cancel_for_computer(
            computer.id, "authorization_revoked"
        )
        request.app.state.agent_connections.send_urgent(
            computer.agent_id,
            {"type": "session.stop", "reason": "authorization_revoked"},
        )
        request.app.state.screen_streams.agent_disconnected(computer.agent_id)
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
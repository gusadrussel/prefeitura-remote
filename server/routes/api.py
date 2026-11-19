"""Authenticated inventory ingestion and technician endpoints."""

import logging
import secrets
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from server.schemas import AuthorizationStatus, ComputerSummary, InventoryAccepted, InventoryPayload
from server.schemas import TechnicianLogin, TechnicianCreate
from server.services.technician_auth import SessionCapacityError
from server.database.models import ActivationCode, Computer
from server.provision_agent import ACTIVATION_CODE_TTL_SECONDS, issue_activation_code
from server.services.inventory import (
    as_utc,
    computer_summary,
    set_authorization_status,
    upsert_inventory,
)
from server.services.rate_limits import peer_address
from server.services.agent_credentials import (
    has_active_agent_credential,
    hash_agent_credential,
    verify_agent_credential,
    token_matches,
)
from server.services.screen_streams import ScreenTicketCapacityError
from server.services.remote_consent import ConsentCapacityError
from server.services.audit import audit_event, audit_available

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
        if token_matches(provided, expected_token):
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


@router.post("/api/v1/agents/enroll")
def enroll_agent(
    request: Request,
    session: Session = Depends(get_session),
):
    """Exchange the one-time deployment bootstrap token for a per-agent credential."""
    settings = request.app.state.settings
    enrollment = request.headers.get("X-Prefeitura-Enrollment", "").strip()
    agent_id = request.headers.get("X-Prefeitura-Agent-Id", "").strip()
    if not settings.enrollment_token:
        raise HTTPException(status_code=503, detail="Enrollment de agentes não configurado")
    rate_key = f"enrollment:{peer_address(request.client)}"
    if not enrollment or not token_matches(enrollment, settings.enrollment_token):
        if not request.app.state.rate_limiter.allow(
            rate_key,
            limit=settings.auth_failures_per_window,
            window_seconds=settings.rate_limit_window_seconds,
        ):
            raise HTTPException(
                status_code=429,
                detail="Muitas tentativas de enrollment; tente novamente mais tarde",
                headers={"Retry-After": str(settings.rate_limit_window_seconds)},
            )
        raise HTTPException(status_code=401, detail="Credencial de enrollment inválida")
    request.app.state.rate_limiter.reset(rate_key)
    try:
        agent_id = str(__import__("uuid").UUID(agent_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="agent_id inválido") from None

    computer = session.scalar(
        select(request.app.state.computer_model).where(
            request.app.state.computer_model.agent_id == agent_id
        )
    )
    if computer is None:
        computer = Computer(
            agent_id=agent_id,
            hostname="PENDING-ENROLLMENT",
            last_seen=datetime(1970, 1, 1, tzinfo=UTC),
            inventory={},
            authorization_status="pending",
        )
        session.add(computer)
    elif (computer.agent_credential_hash is not None or computer.agent_credential_revoked
          or computer.authorization_status != "pending"):
        raise HTTPException(status_code=409, detail="Agente já provisionado; rotação exige administrador")

    credential = secrets.token_urlsafe(32)
    expiry = datetime.now(UTC) + timedelta(days=settings.agent_credential_ttl_days)
    if computer.id is None:
        computer.agent_credential_hash = hash_agent_credential(credential)
        computer.agent_credential_revoked = False
        computer.agent_credential_expires_at = expiry
    else:
        result = session.execute(update(Computer).where(
            Computer.id == computer.id, Computer.agent_credential_hash.is_(None),
            Computer.agent_credential_revoked.is_(False), Computer.authorization_status == "pending",
        ).values(agent_credential_hash=hash_agent_credential(credential),
                 agent_credential_revoked=False, agent_credential_expires_at=expiry))
        if result.rowcount != 1:
            session.rollback()
            raise HTTPException(status_code=409, detail="Provisionamento concorrente; consulte o administrador")
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Agente já provisionado; consulte o administrador") from None

    LOGGER.info("Agente enrolled: %s", agent_id)
    audit_event("agent_enrolled", agent_id=agent_id, outcome="success", peer=peer_address(request.client))
    return {"agent_id": agent_id, "credential": credential}


@router.post("/api/v1/agents/activate", status_code=201)
def activate_agent(request: Request, session: Session = Depends(get_session)):
    settings = request.app.state.settings
    peer = peer_address(request.client)
    if not request.app.state.rate_limiter.allow(
        f"activation:{peer}", limit=settings.auth_failures_per_window,
        window_seconds=settings.rate_limit_window_seconds,
    ):
        raise HTTPException(429, "Muitas tentativas de ativação")
    code = request.headers.get("X-Prefeitura-Activation", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", code):
        raise HTTPException(401, "Código de ativação inválido ou expirado")
    try:
        agent_id = str(uuid.UUID(request.headers.get("X-Prefeitura-Agent-Id", "")))
    except ValueError:
        raise HTTPException(400, "Identidade de agente inválida") from None

    now = datetime.now(UTC)
    consumed = session.execute(delete(ActivationCode).where(
        ActivationCode.digest == hash_agent_credential(code), ActivationCode.expires_at > now,
    ))
    if consumed.rowcount != 1:
        raise HTTPException(401, "Código de ativação inválido ou expirado")

    credential = secrets.token_urlsafe(32)
    session.add(Computer(
        agent_id=agent_id, hostname="PENDING-ACTIVATION",
        last_seen=datetime(1970, 1, 1, tzinfo=UTC), inventory={}, authorization_status="pending",
        agent_credential_hash=hash_agent_credential(credential), agent_credential_revoked=False,
        agent_credential_expires_at=now + timedelta(days=settings.agent_credential_ttl_days),
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Identidade já cadastrada") from None
    audit_event("agent_enrolled", agent_id=agent_id, outcome="success", peer=peer)
    return JSONResponse({"agent_id": agent_id, "credential": credential}, status_code=201,
                        headers={"Cache-Control": "no-store"})


@router.post("/api/v1/admin/agents/{agent_id}/credential")
def provision_agent_credential(
    agent_id: str,
    request: Request,
    _authorized: None = Depends(dashboard_auth),
    session: Session = Depends(get_session),
):
    """Issue or rotate a per-agent credential through the authenticated admin API."""
    computer = session.scalar(
        select(request.app.state.computer_model).where(
            request.app.state.computer_model.agent_id == agent_id
        )
    )
    if computer is None:
        raise HTTPException(status_code=404, detail="Agente não encontrado")

    credential = secrets.token_urlsafe(32)
    computer.agent_credential_hash = hash_agent_credential(credential)
    computer.agent_credential_revoked = False
    settings = request.app.state.settings
    computer.agent_credential_expires_at = datetime.now(UTC) + timedelta(days=settings.agent_credential_ttl_days)
    session.commit()

    LOGGER.info(
        "Credencial individual provisionada para agent_id=%s",
        computer.agent_id,
    )
    audit_event("agent_credential_rotated", agent_id=computer.agent_id, principal="dashboard-admin", principal_type="bootstrap", outcome="success")
    request.app.state.remote_consent.cancel_for_computer(computer.id,"credential_rotated")
    request.app.state.screen_streams.agent_disconnected(computer.agent_id)
    request.app.state.agent_connections.send_urgent(computer.agent_id,{"type":"session.stop","reason":"channel_unavailable"})
    return {
        "agent_id": computer.agent_id,
        "credential": credential,
        "expires_at": computer.agent_credential_expires_at,
        "message": "Guarde esta credencial; ela não será exibida novamente.",
    }


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
        and has_active_agent_credential(computer, settings)
        and verify_agent_credential(provided, computer.agent_credential_hash)
    )
    legacy_valid = bool(
        not has_individual_credential
        and settings.allow_legacy_agent_token
        and settings.agent_token
        and token_matches(provided, settings.agent_token)
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
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    _technician(request, authorization)
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
    authorization: str | None = Header(default=None),
    mode: str = Query(default="view", pattern="^(view|control)$"),
    session: Session = Depends(get_session),
):
    technician = _technician(request, authorization)
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
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    technician = _technician(request, authorization)
    return _update_authorization(session, computer_id, "authorized", request, principal=technician.username,
                                 principal_type="bootstrap" if technician.id == 0 else "technician")


@router.post("/api/v1/admin/computers/{computer_id}/revoke", response_model=ComputerSummary)
def revoke_computer(
    computer_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    technician = _technician(request, authorization)
    return _update_authorization(session, computer_id, "revoked", request, principal=technician.username,
                                 principal_type="bootstrap" if technician.id == 0 else "technician")


@router.post("/api/v1/admin/computers/{computer_id}/screen/access-request", status_code=202)
def create_remote_access_request(
    computer_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    mode: str = Query(default="view", pattern="^(view|control)$"),
    session: Session = Depends(get_session),
):
    technician = _technician(request, authorization)
    if not audit_available(request.app):
        raise HTTPException(status_code=503, detail='Auditoria indisponível')
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
    parent_id = None
    if mode == "control":
        viewer = request.app.state.screen_streams.get(computer.agent_id)
        parent = request.app.state.remote_consent.get(viewer.view_request_id) if viewer else None
        if (viewer is None or viewer.state != "streaming" or viewer.principal != technician.username
                or parent is None or parent.bootstrap != (technician.id == 0)
                or not request.app.state.remote_consent.active(viewer.view_request_id, "view")):
            raise HTTPException(status_code=409, detail="Visualização consentida ativa obrigatória")
        parent_id = viewer.view_request_id
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
            technician.display_name,
            mode=mode,
            ttl_seconds=settings.remote_consent_ttl_seconds,
            principal=technician.username, parent_id=parent_id,
            session_token=(authorization.removeprefix("Bearer ").strip() if authorization and technician.id else None),
            bootstrap=not bool(technician.id),
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
            "mode": consent.mode,
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
    audit_event("remote_consent_requested", principal=technician.username, computer_id=computer.id,
                agent_id=computer.agent_id, request_id=consent.request_id, mode=mode)
    return {
        "request_id": consent.request_id,
        "status": consent.status,
        "technician_name": consent.technician_name,
        "mode": consent.mode,
        "expires_in": settings.remote_consent_ttl_seconds,
    }


@router.post("/api/v1/admin/computers/{computer_id}/screen/access-request/{request_id}/cancel")
def cancel_remote_access_request(
    computer_id: int,
    request_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    technician = _technician(request, authorization)
    consent = request.app.state.remote_consent.get(request_id)
    if (consent is None or consent.computer_id != computer_id or consent.principal != technician.username
            or consent.bootstrap != (technician.id == 0)):
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    request.app.state.remote_consent.cancel_for_computer(computer_id, "technician_cancelled")
    audit_event("remote_consent_cancelled", principal=technician.username, computer_id=computer_id, request_id=request_id)
    request.app.state.screen_streams.agent_disconnected(consent.agent_id)
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
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    technician = _technician(request, authorization)
    consent = request.app.state.remote_consent.get(request_id)
    if (consent is None or consent.computer_id != computer_id or consent.principal != technician.username
            or consent.bootstrap != (technician.id == 0)):
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    computer = session.get(request.app.state.computer_model, computer_id)
    if computer is None:
        raise HTTPException(status_code=404, detail="Computador não encontrado")
    if consent.status == "approved" and consent.ticket is None:
        if consent.mode != "view":
            pass
        elif (
            computer.authorization_status != "authorized"
            or not has_active_agent_credential(computer, request.app.state.settings)
            or not (connection := request.app.state.agent_connections.get(computer.agent_id))
            or not connection.connected
        ):
            request.app.state.remote_consent.end(request_id, "authorization_or_channel_changed")
        else:
            try:
                request.app.state.remote_consent.issue_view_ticket(
                    request_id, request.app.state.screen_streams
                )
            except ScreenTicketCapacityError:
                return {"status": "approved", "technician_name": consent.technician_name}
            LOGGER.info(
                "remote_access_consent_approved technician=%s computer_id=%s agent_id=%s request_id=%s",
                consent.technician_name, computer.id, computer.agent_id, request_id,
            )
    response = {
        "status": consent.status,
        "technician_name": consent.technician_name,
        "mode": consent.mode,
        "reason": consent.reason,
    }
    if consent.ticket is not None and consent.status in {"approved", "active"}:
        response.update({"ticket": consent.ticket, "expires_in": consent.ticket_expires_in})
    return response


def _update_authorization(
    session: Session,
    computer_id: int,
    status: AuthorizationStatus,
    request: Request,
    principal: str,
    principal_type: str,
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
    audit_event("computer_authorization_changed", principal=principal, principal_type=principal_type, computer_id=computer_id, agent_id=computer.agent_id,
                outcome=computer.authorization_status)
    return computer_summary(
        computer,
        request.app.state.settings.online_threshold_seconds,
        request.app.state.agent_connections.get(computer.agent_id),
    )


@router.get("/api/v1/health")
def health():
    return {"status": "ok"}


@router.get('/api/v1/ready')
def readiness(request: Request):
    if not audit_available(request.app):
        raise HTTPException(status_code=503, detail='Auditoria indisponível')
    try:
        from sqlalchemy import text
        with request.app.state.engine.connect() as connection:
            connection.execute(text('SELECT 1'))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail='Banco de dados indisponível') from None
    return {'status':'ready'}


@router.get("/")
def dashboard():
    return FileResponse(Path(__file__).resolve().parents[2] / "dashboard" / "index.html")
# ---- Technician accounts, local computer aliases, chat and tickets ----
from server.database.models import Technician, ComputerMessage, Ticket
from server.services.technician_auth import hash_password, verify_password

def _technician(request: Request, authorization: str | None = Header(default=None)) -> Technician:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    technician_id = request.app.state.technician_sessions.get(token)
    session = request.app.state.session_factory()
    try:
        technician = session.get(Technician, technician_id) if technician_id else None
        if technician and technician.authorized:
            return technician
    finally:
        session.close()
    try:
        dashboard_auth(request, authorization)
        return Technician(
            id=0, username="dashboard-admin", display_name=request.app.state.settings.dashboard_operator_name,
            authorized=True, role="admin", password_hash="", created_at=datetime.now(UTC)
        )
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        raise HTTPException(status_code=401, detail="Sessão de técnico inválida ou expirada") from None


def _require_admin(request: Request, authorization: str | None = Header(default=None)) -> Technician:
    technician = _technician(request, authorization)
    if technician.role != "admin":
        raise HTTPException(status_code=403, detail="Seu perfil não tem permissão para esta ação")
    return technician


@router.get("/api/v1/admin/installation-codes")
def installation_code_policy(_operator: Technician = Depends(_require_admin)):
    return JSONResponse({"expires_in": ACTIVATION_CODE_TTL_SECONDS},
                        headers={"Cache-Control": "no-store"})


@router.post("/api/v1/admin/installation-codes", status_code=201)
def create_installation_code(
    request: Request,
    operator: Technician = Depends(_require_admin),
    session: Session = Depends(get_session),
):
    code = issue_activation_code(session)
    audit_event("installation_code_issued", principal=operator.username,
                principal_type="bootstrap" if operator.id == 0 else "technician",
                outcome="success", peer=peer_address(request.client))
    return JSONResponse({"code": code, "expires_in": ACTIVATION_CODE_TTL_SECONDS}, status_code=201,
                        headers={"Cache-Control": "no-store"})


def _tech_id(request: Request, authorization: str | None) -> int | None:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    return request.app.state.technician_sessions.get(token)

@router.post("/api/v1/auth/login")
def technician_login(payload: TechnicianLogin, request: Request, session: Session = Depends(get_session)):
    settings = request.app.state.settings
    key = f"technician-login:{peer_address(request.client)}"
    if not request.app.state.rate_limiter.allow(key, limit=settings.auth_failures_per_window, window_seconds=settings.rate_limit_window_seconds):
        raise HTTPException(status_code=429, detail="Muitas tentativas de autenticação", headers={"Retry-After":str(settings.rate_limit_window_seconds)})
    password = payload.password.get_secret_value()
    if not 1 <= len(password) <= 1024:
        raise HTTPException(status_code=400, detail="Dados de autenticação inválidos")
    technician = session.scalar(select(Technician).where(Technician.username == payload.username))
    if not technician or not technician.authorized or not verify_password(password, technician.password_hash):
        audit_event("technician_login", outcome="denied", peer=peer_address(request.client))
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
    request.app.state.rate_limiter.reset(key)
    try:
        token = request.app.state.technician_sessions.issue(technician.id)
    except SessionCapacityError:
        raise HTTPException(status_code=429, detail="Limite de sessões ativas atingido") from None
    audit_event("technician_login", principal=technician.username, technician_id=technician.id, outcome="success")
    return {"token": token, "expires_in": request.app.state.technician_sessions._ttl_seconds, "technician": {"id": technician.id, "username": technician.username, "display_name": technician.display_name, "role": technician.role}}

@router.get("/api/v1/admin/technicians")
def list_technicians(request: Request, authorization: str | None = Header(default=None), session: Session = Depends(get_session)):
    _technician(request, authorization)
    return [{"id": t.id, "username": t.username, "display_name": t.display_name, "authorized": t.authorized, "role": t.role, "created_at": t.created_at} for t in session.scalars(select(Technician).order_by(Technician.display_name)).all()]


@router.post("/api/v1/auth/logout")
def technician_logout(request: Request, authorization: str | None = Header(default=None)):
    technician = _technician(request, authorization)
    if technician.id:
        token = authorization.removeprefix("Bearer ").strip()
        request.app.state.technician_sessions.revoke(token)
        for agent_id in request.app.state.remote_consent.cancel_session(token):
            request.app.state.screen_streams.agent_disconnected(agent_id)
            request.app.state.agent_connections.send_urgent(agent_id, {"type":"session.remote_stop", "reason":"technician_logout"})
    audit_event("technician_logout", principal=technician.username, technician_id=technician.id)
    return {"status":"logged_out"}

@router.post("/api/v1/admin/technicians")
def create_technician(payload: TechnicianCreate, request: Request, authorization: str | None = Header(default=None), session: Session = Depends(get_session)):
    operator = _require_admin(request, authorization)
    username, display_name, password = payload.username, payload.display_name, payload.password.get_secret_value()
    if len(username.strip()) < 3 or not 10 <= len(password) <= 1024 or not display_name.strip(): raise HTTPException(status_code=400, detail="Dados da conta inválidos")
    if session.scalar(select(Technician).where(Technician.username == username.strip())): raise HTTPException(status_code=409, detail="Usuário já existe")
    technician = Technician(username=username.strip(), display_name=display_name.strip(), password_hash=hash_password(password), authorized=True, created_at=datetime.now(UTC))
    session.add(technician)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Usuário já existe") from None
    session.refresh(technician)
    audit_event("technician_created",principal=operator.username,principal_type="bootstrap" if operator.id == 0 else "technician",technician_id=technician.id)
    return {"id": technician.id, "username": technician.username, "display_name": technician.display_name, "authorized": technician.authorized, "role": technician.role}

@router.post("/api/v1/admin/technicians/{technician_id}/revoke")
def revoke_technician(technician_id: int, request: Request, authorization: str | None = Header(default=None), session: Session = Depends(get_session)):
    operator = _require_admin(request, authorization)
    technician = session.get(Technician, technician_id)
    if not technician: raise HTTPException(status_code=404, detail="Técnico não encontrado")
    technician.authorized = False; session.commit()
    request.app.state.technician_sessions.revoke_technician(technician.id)
    for agent_id in request.app.state.remote_consent.cancel_principal(technician.username):
        request.app.state.screen_streams.agent_disconnected(agent_id)
        request.app.state.agent_connections.send_urgent(agent_id, {"type":"session.remote_stop", "reason":"technician_revoked"})
    audit_event("technician_revoked", principal=operator.username, principal_type="bootstrap" if operator.id == 0 else "technician", technician_id=technician.id)
    return {"status": "revoked"}

@router.patch("/api/v1/admin/computers/{computer_id}/name")
def rename_computer(computer_id: int, request: Request, name: str, authorization: str | None = Header(default=None), session: Session = Depends(get_session)):
    _technician(request, authorization)
    computer = session.get(Computer, computer_id)
    if not computer: raise HTTPException(status_code=404, detail="Computador não encontrado")
    name = name.strip()
    if not 1 <= len(name) <= 255 or "\x00" in name: raise HTTPException(status_code=400, detail="Nome inválido")
    computer.display_name = name; session.commit(); session.refresh(computer)
    return computer_summary(computer, request.app.state.settings.online_threshold_seconds, request.app.state.agent_connections.get(computer.agent_id))

@router.get("/api/v1/admin/computers/{computer_id}/messages")
def list_messages(computer_id: int, request: Request, authorization: str | None = Header(default=None), session: Session = Depends(get_session), limit: int = Query(default=200, ge=1, le=500), before_id: int | None = Query(default=None, ge=1)):
    _technician(request, authorization)
    if session.get(Computer, computer_id) is None:
        raise HTTPException(status_code=404, detail="Computador não encontrado")
    statement = select(ComputerMessage).where(ComputerMessage.computer_id == computer_id)
    if before_id is not None:
        statement = statement.where(ComputerMessage.id < before_id)
    rows = session.scalars(statement.order_by(ComputerMessage.id.desc()).limit(limit)).all()
    return [{"id": m.id, "sender": m.sender, "message": m.message, "created_at": m.created_at} for m in reversed(rows)]

@router.post("/api/v1/admin/computers/{computer_id}/messages")
def send_message(computer_id: int, request: Request, message: str = Body(embed=True), authorization: str | None = Header(default=None), session: Session = Depends(get_session)):
    tech = _technician(request, authorization)
    computer = session.get(Computer, computer_id)
    if not computer: raise HTTPException(status_code=404, detail="Computador não encontrado")
    message = message.strip()
    if not message or len(message) > 2000 or "\x00" in message: raise HTTPException(status_code=400, detail="Mensagem inválida")
    tech_id = tech.id or None
    row = ComputerMessage(computer_id=computer_id, technician_id=tech_id, sender="technician", message=message, created_at=datetime.now(UTC))
    session.add(row); session.commit(); session.refresh(row)
    if request.app.state.agent_connections.send(computer.agent_id, {"type":"chat.message","message_id":row.id,"sender":"technician","message":message}):
        return {"id": row.id, "delivered": True}
    return {"id": row.id, "delivered": False}



@router.get("/api/v1/admin/tickets")
def list_tickets(request: Request, authorization: str | None = Header(default=None), status: str | None = None, session: Session = Depends(get_session), limit: int = Query(default=200, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    _technician(request, authorization)
    stmt = select(Ticket).order_by(Ticket.updated_at.desc(), Ticket.id.desc())
    if status: stmt = stmt.where(Ticket.status == status)
    return [{"id":t.id,"computer_id":t.computer_id,"technician_id":t.technician_id,"title":t.title,"description":t.description,"status":t.status,"priority":t.priority,"created_at":t.created_at,"updated_at":t.updated_at} for t in session.scalars(stmt.limit(limit).offset(offset)).all()]

@router.post("/api/v1/admin/tickets")
def create_ticket(request: Request, title: str, description: str, computer_id: int | None = None, priority: str = "normal", authorization: str | None = Header(default=None), session: Session = Depends(get_session)):
    tech = _technician(request, authorization)
    if not title.strip() or not description.strip(): raise HTTPException(status_code=400, detail="Título e descrição são obrigatórios")
    if len(title.strip()) > 200 or len(description) > 20_000:
        raise HTTPException(status_code=400, detail="Título ou descrição excede o limite")
    if "\x00" in title or "\x00" in description:
        raise HTTPException(status_code=400, detail="Título ou descrição contém caractere inválido")
    if computer_id is not None and session.get(Computer, computer_id) is None:
        raise HTTPException(status_code=404, detail="Computador não encontrado")
    if priority not in {"low","normal","high","urgent"}: raise HTTPException(status_code=400, detail="Prioridade inválida")
    now=datetime.now(UTC); row=Ticket(computer_id=computer_id, technician_id=tech.id or None,title=title.strip(),description=description.strip(),priority=priority,status="open",created_at=now,updated_at=now)
    session.add(row); session.commit(); session.refresh(row)
    return {"id":row.id,"status":row.status}

@router.patch("/api/v1/admin/tickets/{ticket_id}")
def update_ticket(ticket_id: int, request: Request, status: str, authorization: str | None = Header(default=None), session: Session = Depends(get_session)):
    _technician(request, authorization); row=session.get(Ticket,ticket_id)
    if not row: raise HTTPException(status_code=404, detail="Chamado não encontrado")
    if status not in {"open","in_progress","waiting","resolved","closed"}: raise HTTPException(status_code=400, detail="Status inválido")
    row.status=status; row.updated_at=datetime.now(UTC); session.commit(); return {"id":row.id,"status":row.status}

"""Offline operator CLI for issuing and revoking per-agent credentials.

Run on the server host with access to the configured database. Issued credentials
are printed once to stdout and are never stored in the database.
"""

import argparse
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.config import load_settings
from server.database.models import Computer
from server.database.session import create_database
from server.services.agent_credentials import hash_agent_credential


def provision_agent(session: Session, agent_id: str) -> tuple[Computer, str]:
    computer = session.scalar(select(Computer).where(Computer.agent_id == agent_id))
    if computer is None:
        computer = Computer(
            agent_id=agent_id,
            hostname="PENDING-PROVISIONING",
            last_seen=datetime(1970, 1, 1, tzinfo=UTC),
            inventory={},
            authorization_status="pending",
        )
        session.add(computer)

    credential = secrets.token_urlsafe(32)
    computer.agent_credential_hash = hash_agent_credential(credential)
    computer.agent_credential_revoked = False
    session.commit()
    session.refresh(computer)
    return computer, credential


def revoke_agent_credential(session: Session, agent_id: str) -> bool:
    computer = session.scalar(select(Computer).where(Computer.agent_id == agent_id))
    if computer is None:
        return False
    computer.agent_credential_revoked = True
    session.commit()
    return True


def _validated_agent_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("agent_id deve ser um UUID válido") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provisionamento offline de credenciais de agente")
    subparsers = parser.add_subparsers(dest="action", required=True)
    issue_parser = subparsers.add_parser("issue", help="Emitir ou rotacionar uma credencial")
    issue_parser.add_argument("agent_id", type=_validated_agent_id)
    revoke_parser = subparsers.add_parser("revoke", help="Revogar individualmente uma credencial")
    revoke_parser.add_argument("agent_id", type=_validated_agent_id)
    arguments = parser.parse_args(argv)

    settings = load_settings()
    engine, session_factory = create_database(settings.database_url)
    try:
        with session_factory() as session:
            if arguments.action == "issue":
                computer, credential = provision_agent(session, arguments.agent_id)
                print(f"agent_id={computer.agent_id}")
                print(f"credential={credential}")
                print("Configure PREFEITURA_AGENT_CREDENTIAL no agente por um canal seguro.")
                print("A credencial é exibida somente nesta emissão; proteja este terminal.")
            elif revoke_agent_credential(session, arguments.agent_id):
                print(f"Credencial revogada para agent_id={arguments.agent_id}")
            else:
                print("Agente não encontrado")
                return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

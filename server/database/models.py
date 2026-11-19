"""SQLAlchemy entities for registered endpoint computers."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, ForeignKey, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Computer(Base):
    __tablename__ = "computers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255), index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    logged_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    os_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    os_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    inventory: Mapped[dict] = mapped_column(JSON)
    authorization_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'")
    )
    agent_credential_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_credential_revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    agent_credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class ActivationCode(Base):
    __tablename__ = "activation_codes"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Technician(Base): 
    __tablename__ = "technicians"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(300))
    authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("TRUE"))
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="technician", server_default=text("'technician'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class ComputerMessage(Base):
    __tablename__ = "computer_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    computer_id: Mapped[int] = mapped_column(ForeignKey("computers.id"), index=True)
    technician_id: Mapped[int | None] = mapped_column(ForeignKey("technicians.id"), nullable=True)
    sender: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class Ticket(Base): 
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    computer_id: Mapped[int | None] = mapped_column(ForeignKey("computers.id"), nullable=True, index=True)
    technician_id: Mapped[int | None] = mapped_column(ForeignKey("technicians.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="open", server_default=text("'open'"))
    priority: Mapped[str] = mapped_column(String(20), default="normal", server_default=text("'normal'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

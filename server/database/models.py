"""SQLAlchemy entities for registered endpoint computers."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Computer(Base):
    __tablename__ = "computers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255), index=True)
    logged_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    os_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    os_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    inventory: Mapped[dict] = mapped_column(JSON)
    authorization_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'authorized'")
    )
"""SQLAlchemy engine and per-request session setup."""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from server.database.models import Base


def create_database(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    _migrate_authorization_status(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _migrate_authorization_status(engine) -> None:
    with engine.begin() as connection:
        if "computers" not in inspect(connection).get_table_names():
            return
        columns = {column["name"] for column in inspect(connection).get_columns("computers")}
        if "authorization_status" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE computers ADD COLUMN authorization_status "
                    "VARCHAR(20) NOT NULL DEFAULT 'authorized'"
                )
            )
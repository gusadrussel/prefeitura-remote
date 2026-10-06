"""SQLAlchemy engine and per-request session setup."""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from server.database.models import Base

AUTHORIZATION_MIGRATION = "legacy_authorized_default_to_pending_v1"
MIGRATION_TABLE = "prefeitura_remote_migrations"


def create_database(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    _migrate_authorization_status(engine)
    _migrate_agent_credentials(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _migrate_authorization_status(engine) -> None:
    with engine.begin() as connection:
        if "computers" not in inspect(connection).get_table_names():
            return
        columns = inspect(connection).get_columns("computers")
        authorization_column = next(
            (column for column in columns if column["name"] == "authorization_status"),
            None,
        )
        if authorization_column is None:
            connection.execute(
                text(
                    "ALTER TABLE computers ADD COLUMN authorization_status "
                    "VARCHAR(20) NOT NULL DEFAULT 'pending'"
                )
            )
            return

        old_default = str(authorization_column.get("default") or "").lower()
        if "authorized" not in old_default:
            return

        connection.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} "
                "(revision VARCHAR(100) PRIMARY KEY)"
            )
        )
        applied = connection.execute(
            text(f"SELECT 1 FROM {MIGRATION_TABLE} WHERE revision = :revision"),
            {"revision": AUTHORIZATION_MIGRATION},
        ).scalar_one_or_none()
        if applied:
            return

        connection.execute(
            text(
                "UPDATE computers SET authorization_status = 'pending' "
                "WHERE authorization_status = 'authorized'"
            )
        )
        connection.execute(
            text(f"INSERT INTO {MIGRATION_TABLE} (revision) VALUES (:revision)"),
            {"revision": AUTHORIZATION_MIGRATION},
        )
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "ALTER TABLE computers ALTER COLUMN authorization_status "
                    "SET DEFAULT 'pending'"
                )
            )


def _migrate_agent_credentials(engine) -> None:
    with engine.begin() as connection:
        if "computers" not in inspect(connection).get_table_names():
            return
        columns = {column["name"] for column in inspect(connection).get_columns("computers")}
        if "agent_credential_hash" not in columns:
            connection.execute(
                text("ALTER TABLE computers ADD COLUMN agent_credential_hash VARCHAR(64)")
            )
        if "agent_credential_revoked" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE computers ADD COLUMN agent_credential_revoked "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
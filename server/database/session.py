"""SQLAlchemy engine and per-request session setup."""

from contextlib import contextmanager
from sqlalchemy import DateTime, create_engine, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import sessionmaker

from server.database.models import Base

AUTHORIZATION_MIGRATION = "legacy_authorized_default_to_pending_v1"
MIGRATION_TABLE = "prefeitura_remote_migrations"


class SchemaNotReadyError(RuntimeError):
    pass


def create_database(database_url: str, *, migrate: bool = True, pool_size: int = 3,
                    max_overflow: int = 2, pool_timeout: float = 5, sqlite_journal_mode: str = ''):
    if sqlite_journal_mode not in {'','DELETE','PERSIST'}:
        raise ValueError('SQLite journal mode must be empty, DELETE or PERSIST')
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    options = {}
    if database_url.startswith("postgresql"):
        connect_args = {"connect_timeout": 5,
            "options": "-c timezone=UTC -c statement_timeout=15000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=30000"}
        options = {"pool_size":pool_size,"max_overflow":max_overflow,"pool_timeout":pool_timeout,"pool_recycle":1800}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True,
                           hide_parameters=True, **options)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_constraints(connection, record):
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                if sqlite_journal_mode:
                    cursor.execute(f'PRAGMA journal_mode={sqlite_journal_mode}')
                    if cursor.fetchone()[0].upper() != sqlite_journal_mode:
                        raise RuntimeError('Requested SQLite journal mode was not applied')
                    cursor.execute('PRAGMA synchronous=FULL')
            finally:
                cursor.close()
    try:
        if migrate:
            initialize_schema(engine)
        else:
            verify_schema(engine)
    except BaseException:
        # Construction failed before ownership could be handed to the app.
        engine.dispose()
        raise
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def initialize_schema(engine):
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            # sqlite3 legacy transaction control otherwise auto-commits DDL.
            connection.exec_driver_sql("BEGIN")
        elif engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(728413601)"))
        Base.metadata.create_all(connection)
        _migrate_authorization_status(connection)
        _migrate_agent_credentials(connection)
        _migrate_computer_display_name(connection)
        _migrate_agent_credential_expiry(connection)
        _migrate_technician_role(connection)
        verify_schema(connection)


def verify_schema(engine):
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            raise SchemaNotReadyError("Esquema ausente: execute a migração administrativa antes do serviço")
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        if not set(table.columns.keys()) <= columns:
            raise SchemaNotReadyError("Esquema desatualizado: migração administrativa necessária")
    authorization = next(c for c in inspector.get_columns("computers") if c["name"] == "authorization_status")
    if "authorized" in str(authorization.get("default", "")).lower():
        if MIGRATION_TABLE not in tables:
            raise SchemaNotReadyError("Correção de autorização legada ainda não aplicada")
        with _migration_connection(engine) as connection:
            applied = connection.execute(text(f"SELECT 1 FROM {MIGRATION_TABLE} WHERE revision=:revision"),
                                         {"revision":AUTHORIZATION_MIGRATION}).scalar_one_or_none()
        if not applied:
            raise SchemaNotReadyError("Correção de autorização legada ainda não aplicada")
    if engine.dialect.name == "sqlite":
        with _migration_connection(engine) as connection:
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                raise SchemaNotReadyError("Integridade referencial inválida; revise uma cópia isolada antes de iniciar")


@contextmanager
def _migration_connection(target):
    if isinstance(target, Connection):
        yield target
    else:
        with target.begin() as connection:
            yield connection


def _migrate_authorization_status(engine) -> None:
    with _migration_connection(engine) as connection:
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
    with _migration_connection(engine) as connection:
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

# New tables are created automatically by Base.metadata.create_all; this hook exists
# to document that these are additive migrations and require no destructive changes.


def _migrate_computer_display_name(engine) -> None:
    with _migration_connection(engine) as connection:
        if "computers" not in inspect(connection).get_table_names(): return
        columns = {column["name"] for column in inspect(connection).get_columns("computers")}
        if "display_name" not in columns:
            connection.execute(text("ALTER TABLE computers ADD COLUMN display_name VARCHAR(255)"))


def _migrate_agent_credential_expiry(engine) -> None:
    with _migration_connection(engine) as connection:
        if "computers" not in inspect(connection).get_table_names():
            return
        columns = {column["name"] for column in inspect(connection).get_columns("computers")}
        if "agent_credential_expires_at" not in columns:
            column_type = DateTime(timezone=True).compile(dialect=engine.dialect)
            connection.execute(text(f"ALTER TABLE computers ADD COLUMN agent_credential_expires_at {column_type}"))

def _migrate_technician_role(engine) -> None:
    with _migration_connection(engine) as connection:
        if "technicians" not in inspect(connection).get_table_names():
            return
        columns = {column["name"] for column in inspect(connection).get_columns("technicians")}
        if "role" not in columns:
            connection.execute(text("ALTER TABLE technicians ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'technician'"))

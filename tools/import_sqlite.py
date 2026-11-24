"""Import a verified SQLite backup into an initialized, empty PostgreSQL database."""

import argparse
from datetime import UTC
import os
from pathlib import Path
import sqlite3
import sys

from sqlalchemy import DateTime, create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from server.database.models import Base
from server.database.session import SchemaNotReadyError, verify_schema


PERSISTENT_TABLES = tuple(table for table in Base.metadata.sorted_tables if table.name != "activation_codes")


def require_empty_target(connection):
    for table in Base.metadata.sorted_tables:
        if connection.scalar(select(table.primary_key.columns[0]).limit(1)) is not None:
            raise ValueError("O destino precisa estar vazio em todas as tabelas da aplicação.")


def copy_snapshot(source, target):
    """Copy the durable rows in dependency order inside caller-owned transactions."""
    counts = {}
    for table in PERSISTENT_TABLES:
        timestamps = [column.name for column in table.columns if isinstance(column.type, DateTime)]
        rows = source.execute(select(table).order_by(table.c.id)).mappings()
        count = 0
        for batch in rows.partitions(250):
            payload = []
            for original in batch:
                row = dict(original)
                for name in timestamps:
                    if row[name] is not None and row[name].tzinfo is None:
                        row[name] = row[name].replace(tzinfo=UTC)
                payload.append(row)
            target.execute(table.insert(), payload)
            count += len(payload)
        if target.scalar(select(func.count()).select_from(table)) != count:
            raise RuntimeError("Contagem divergente no destino.")
        counts[table.name] = count
    return counts


def import_backup(path, database_url, expected_database):
    source_uri = Path(path).resolve(strict=True).as_uri() + "?mode=ro"
    target_url = make_url(database_url)
    if (target_url.drivername != "postgresql+psycopg" or not target_url.host
            or target_url.database != expected_database or target_url.query.get("sslmode") != "verify-full"):
        raise ValueError("Use a URL PostgreSQL explícita do destino com sslmode=verify-full.")

    source_engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(source_uri, uri=True),
                                  hide_parameters=True)
    target_engine = create_engine(target_url, hide_parameters=True, connect_args={"connect_timeout": 10})
    try:
        with source_engine.connect() as source, target_engine.begin() as target:
            source.exec_driver_sql("BEGIN")
            verify_schema(source)
            if source.exec_driver_sql("PRAGMA quick_check").scalar() != "ok":
                raise ValueError("O backup SQLite não passou na verificação de integridade.")
            if target.scalar(text("SELECT current_database()")) != expected_database:
                raise ValueError("O banco conectado diverge do destino autorizado.")
            verify_schema(target)
            target.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            table_names = ", ".join(table.name for table in Base.metadata.sorted_tables)
            target.exec_driver_sql(f"LOCK TABLE {table_names} IN SHARE ROW EXCLUSIVE MODE")
            require_empty_target(target)
            counts = copy_snapshot(source, target)
            for table in PERSISTENT_TABLES:
                highest_id = target.scalar(select(func.max(table.c.id)))
                if highest_id is not None:
                    if target.scalar(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :id, true)"),
                                     {"table": table.name, "id": highest_id}) is None:
                        raise ValueError("Sequência de identidade ausente no destino.")
        return counts
    finally:
        source_engine.dispose()
        target_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-backup", required=True)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--apply", required=True, action="store_true")
    args = parser.parse_args()
    try:
        counts = import_backup(args.sqlite_backup, os.environ.get("PREFEITURA_DATABASE_URL", ""),
                               args.target_database)
    except (SQLAlchemyError, sqlite3.Error, OSError, SchemaNotReadyError, ValueError, RuntimeError) as error:
        print(f"Importação interrompida ({type(error).__name__}). Confira o backup, o esquema e o acesso ao destino.",
              file=sys.stderr)
        return 1
    for table, count in counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit schema maintenance. No database is opened by importing this module."""
import argparse

from server.config import load_settings
from server.database.session import create_database


def main():
    parser = argparse.ArgumentParser(description="Verifica esquema; --apply executa migrações. Faça backup primeiro.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    engine, _ = create_database(settings.database_url, migrate=args.apply,
                               sqlite_journal_mode=settings.sqlite_journal_mode)
    engine.dispose()
    print("Schema applied" if args.apply else "Schema verified")


if __name__ == "__main__":
    main()

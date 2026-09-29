import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.database.models import Computer
from server.database.session import create_database


class AuthorizationMigrationTests(unittest.TestCase):
    def test_legacy_computers_remain_authorized_after_schema_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(database_path)
            connection.execute(
                """CREATE TABLE computers (
                    id INTEGER PRIMARY KEY,
                    agent_id VARCHAR(36) NOT NULL,
                    hostname VARCHAR(255) NOT NULL,
                    logged_user VARCHAR(255),
                    os_name VARCHAR(100),
                    os_version TEXT,
                    agent_version VARCHAR(50),
                    last_seen DATETIME NOT NULL,
                    inventory JSON NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO computers (agent_id, hostname, last_seen, inventory) "
                "VALUES (?, ?, ?, ?)",
                ("a0a0a0a0-1111-4222-8333-123456789abc", "LEGACY-01", "2026-01-01", "{}"),
            )
            connection.commit()
            connection.close()

            engine, _ = create_database(f"sqlite:///{database_path}")
            with Session(engine) as session:
                computer = session.scalar(select(Computer).where(Computer.hostname == "LEGACY-01"))
                self.assertIsNotNone(computer)
                self.assertEqual(computer.authorization_status, "authorized")
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
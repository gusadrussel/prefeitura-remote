import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.database.models import Computer
from server.database.session import create_database
from server.config import Settings
from server.main import create_app
from server.services.inventory import is_authorized


class AuthorizationMigrationTests(unittest.TestCase):
    def test_legacy_computers_become_pending_without_losing_inventory(self):
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
                (
                    "a0a0a0a0-1111-4222-8333-123456789abc",
                    "LEGACY-01",
                    "2026-01-01",
                    '{"cpu":{"model":"preserved-cpu"}}',
                ),
            )
            connection.commit()
            connection.close()

            database_url = f"sqlite:///{database_path}"
            engine, _ = create_database(database_url)
            with Session(engine) as session:
                computer = session.scalar(select(Computer).where(Computer.hostname == "LEGACY-01"))
                self.assertIsNotNone(computer)
                self.assertEqual(computer.agent_id, "a0a0a0a0-1111-4222-8333-123456789abc")
                self.assertEqual(computer.inventory["cpu"]["model"], "preserved-cpu")
                self.assertEqual(computer.authorization_status, "pending")
                self.assertIsNone(computer.agent_credential_hash)
                self.assertFalse(computer.agent_credential_revoked)
                self.assertFalse(is_authorized(computer))
            engine.dispose()

            app = create_app(
                Settings(
                    database_url=database_url,
                    agent_token="agent-test-token",
                    dashboard_token="dashboard-test-token",
                )
            )
            client = TestClient(app, client=("127.0.0.1", 50000))
            dashboard_headers = {"Authorization": "Bearer dashboard-test-token"}
            try:
                listed = client.get("/api/v1/admin/computers", headers=dashboard_headers)
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()[0]["authorization_status"], "pending")
                ticket_response = client.post(
                    "/api/v1/admin/computers/1/screen/access-request",
                    headers=dashboard_headers,
                )
                self.assertEqual(ticket_response.status_code, 403)

                ticket, _ = app.state.screen_streams.issue_ticket(
                    "a0a0a0a0-1111-4222-8333-123456789abc", 1
                )
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(
                        f"ws://127.0.0.1/ws/admin/computers/1/screen?ticket={ticket}"
                    ):
                        pass
                self.assertIsNone(
                    app.state.screen_streams.get("a0a0a0a0-1111-4222-8333-123456789abc")
                )
            finally:
                client.close()
                app.state.engine.dispose()

    def test_existing_authorized_default_is_repaired_once(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "already-migrated.db"
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
                    inventory JSON NOT NULL,
                    authorization_status VARCHAR(20) NOT NULL DEFAULT 'authorized'
                )"""
            )
            connection.execute(
                "INSERT INTO computers "
                "(agent_id, hostname, last_seen, inventory) VALUES (?, ?, ?, ?)",
                ("c0c0c0c0-1111-4222-8333-123456789abc", "OLD-AUTH-01", "2026-01-01", "{}"),
            )
            connection.execute(
                "INSERT INTO computers "
                "(agent_id, hostname, last_seen, inventory, authorization_status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("d0d0d0d0-1111-4222-8333-123456789abc", "OLD-REVOKED-01", "2026-01-01", "{}", "revoked"),
            )
            connection.commit()
            connection.close()

            database_url = f"sqlite:///{database_path}"
            engine, session_factory = create_database(database_url)
            with Session(engine) as session:
                legacy = session.scalar(select(Computer).where(Computer.hostname == "OLD-AUTH-01"))
                revoked = session.scalar(select(Computer).where(Computer.hostname == "OLD-REVOKED-01"))
                self.assertEqual(legacy.authorization_status, "pending")
                self.assertEqual(revoked.authorization_status, "revoked")
                legacy_id = legacy.id
                self.assertEqual(legacy.agent_id, "c0c0c0c0-1111-4222-8333-123456789abc")
                legacy.authorization_status = "authorized"
                session.commit()

                new_computer = Computer(
                    agent_id="e0e0e0e0-1111-4222-8333-123456789abc",
                    hostname="NEW-AFTER-MIGRATION",
                    last_seen=datetime.now(timezone.utc),
                    inventory={},
                )
                session.add(new_computer)
                session.commit()
                session.refresh(new_computer)
                self.assertEqual(new_computer.authorization_status, "pending")
            engine.dispose()

            engine, _ = create_database(database_url)
            with Session(engine) as session:
                approved = session.get(Computer, legacy_id)
                self.assertEqual(approved.authorization_status, "authorized")
                self.assertEqual(approved.hostname, "OLD-AUTH-01")
            engine.dispose()

    def test_new_database_defaults_computers_to_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "new.db"
            engine, _ = create_database(f"sqlite:///{database_path}")
            with Session(engine) as session:
                computer = Computer(
                    agent_id="b0b0b0b0-1111-4222-8333-123456789abc",
                    hostname="NEW-01",
                    last_seen=datetime.now(),
                    inventory={},
                )
                session.add(computer)
                session.commit()
                session.refresh(computer)
                self.assertEqual(computer.authorization_status, "pending")
                self.assertFalse(is_authorized(computer))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
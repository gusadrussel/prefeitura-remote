from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from server.config import Settings
from server.database.models import ActivationCode, Computer, ComputerMessage, Technician, Ticket
from server.database.session import create_database
from server.services.agent_credentials import has_active_agent_credential, hash_agent_credential
from tools.import_sqlite import copy_snapshot, import_backup, require_empty_target


class SqliteImportTests(unittest.TestCase):
    def test_copy_retains_approval_and_links_across_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            source_url = f"sqlite:///{Path(directory, 'source.db').as_posix()}"
            target_url = f"sqlite:///{Path(directory, 'target.db').as_posix()}"
            source, sessions = create_database(source_url)
            target, _ = create_database(target_url)
            now = datetime.now(UTC)
            digest = hash_agent_credential("synthetic-test-credential")
            try:
                with sessions.begin() as session:
                    session.add_all([
                        Computer(id=7, agent_id="00000000-0000-4000-8000-000000000001", hostname="TEST",
                                 authorization_status="authorized", inventory={"synthetic": True}, last_seen=now,
                                 agent_credential_hash=digest, agent_credential_expires_at=now + timedelta(days=1)),
                        Technician(id=8, username="test", display_name="Test", password_hash="synthetic-hash",
                                   created_at=now),
                        ActivationCode(digest="synthetic-digest", expires_at=now + timedelta(minutes=10)),
                    ])
                    session.flush()
                    session.add_all([
                        ComputerMessage(id=9, computer_id=7, technician_id=8, sender="technician",
                                        message="Synthetic chat", created_at=now),
                        Ticket(id=10, computer_id=7, technician_id=8, title="Synthetic ticket",
                               description="Synthetic", created_at=now, updated_at=now),
                    ])
                with source.connect() as snapshot, target.begin() as connection:
                    require_empty_target(connection)
                    self.assertEqual(copy_snapshot(snapshot, connection),
                                     {"computers": 1, "technicians": 1, "computer_messages": 1, "tickets": 1})
            finally:
                source.dispose()
                target.dispose()

            reopened, sessions = create_database(target_url, migrate=False)
            try:
                with sessions() as session:
                    computer = session.get(Computer, 7)
                    self.assertEqual(computer.authorization_status, "authorized")
                    self.assertEqual(computer.agent_credential_hash, digest)
                    self.assertTrue(has_active_agent_credential(
                        computer, Settings(database_url=target_url, agent_token="", dashboard_token="")))
                    self.assertEqual(session.get(ComputerMessage, 9).computer_id, 7)
                    self.assertEqual(session.get(Ticket, 10).technician_id, 8)
                    self.assertIsNone(session.scalar(select(ActivationCode)))
                with reopened.connect() as connection:
                    with self.assertRaisesRegex(ValueError, "destino precisa estar vazio"):
                        require_empty_target(connection)
            finally:
                reopened.dispose()

    def test_wrong_destination_or_unverified_tls_fails_before_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "snapshot.db")
            path.touch()
            for url in ("postgresql+psycopg://test.invalid/wrong?sslmode=verify-full",
                        "postgresql+psycopg://test.invalid/expected?sslmode=require",
                        "sqlite:///other.db"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    import_backup(path, url, "expected")

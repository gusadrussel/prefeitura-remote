import unittest
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.orm import Session

from server.database.models import Computer
from server.database.session import create_database
from server.provision_agent import provision_agent, revoke_agent_credential
from server.services.inventory import is_online
from server.services.agent_credentials import has_active_agent_credential
from server.services.agent_credentials import (
    hash_agent_credential,
    verify_agent_credential,
)


class AgentCredentialHashTests(unittest.TestCase):
    def test_naive_sqlite_expiry_is_utc_not_local_time(self):
        # On a UTC-3 server, astimezone() on a naive value incorrectly extends
        # this expired credential for another three hours.
        class SqliteTimestamp(datetime):
            def astimezone(self, tz=None):
                if self.tzinfo is None:
                    raise AssertionError("Naive SQLite timestamp treated as local time")
                return super().astimezone(tz)
        computer = type("Computer", (), {"agent_credential_revoked":False,
            "agent_credential_hash":"hash", "agent_credential_expires_at":SqliteTimestamp(2026, 1, 1, 11)})()
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 1, 1, 12, tzinfo=UTC)
        with patch("server.services.agent_credentials.datetime", Clock):
            self.assertFalse(has_active_agent_credential(computer, object()))

    def test_hash_verifies_individual_credential_without_storing_plaintext(self):
        credential = "a" * 43
        credential_hash = hash_agent_credential(credential)

        self.assertNotEqual(credential_hash, credential)
        self.assertEqual(len(credential_hash), 64)
        self.assertTrue(verify_agent_credential(credential, credential_hash))
        self.assertFalse(verify_agent_credential("b" * 43, credential_hash))
        self.assertFalse(verify_agent_credential(credential, None))

    def test_provision_rotation_and_revocation_are_individual_and_preserve_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, session_factory = create_database(
                f"sqlite:///{Path(directory) / 'credentials.db'}"
            )
            try:
                agent_id = "a0a0a0a0-1111-4222-8333-123456789abc"
                with Session(engine) as session:
                    computer = Computer(
                        agent_id=agent_id,
                        hostname="AUTHORIZED-01",
                        last_seen=datetime.now(UTC),
                        inventory={},
                        authorization_status="authorized",
                    )
                    session.add(computer)
                    session.commit()
                    computer, first_credential = provision_agent(session, agent_id)
                    first_hash = computer.agent_credential_hash
                    self.assertTrue(verify_agent_credential(first_credential, first_hash))
                    self.assertEqual(computer.authorization_status, "authorized")

                    self.assertTrue(revoke_agent_credential(session, agent_id))
                    session.refresh(computer)
                    self.assertTrue(computer.agent_credential_revoked)
                    self.assertEqual(computer.agent_credential_hash, first_hash)

                    computer, rotated_credential = provision_agent(session, agent_id)
                    self.assertNotEqual(first_credential, rotated_credential)
                    self.assertFalse(computer.agent_credential_revoked)
                    self.assertTrue(
                        verify_agent_credential(rotated_credential, computer.agent_credential_hash)
                    )
                    self.assertFalse(
                        verify_agent_credential(first_credential, computer.agent_credential_hash)
                    )
                    self.assertEqual(computer.authorization_status, "authorized")
            finally:
                engine.dispose()

    def test_expired_credential_is_not_active(self):
        computer = Computer(
            agent_id="c0c0c0c0-1111-4222-8333-123456789abc", hostname="EXPIRED-01",
            last_seen=datetime.now(UTC), inventory={}, authorization_status="authorized",
            agent_credential_hash=hash_agent_credential("secret"),
            agent_credential_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        settings = type("Settings", (), {"allow_legacy_agent_token": False, "agent_token": ""})()
        self.assertFalse(has_active_agent_credential(computer, settings))

    def test_new_provisioned_agent_starts_pending_and_only_hash_is_stored(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, _ = create_database(f"sqlite:///{Path(directory) / 'new-agent.db'}")
            try:
                agent_id = "b0b0b0b0-1111-4222-8333-123456789abc"
                with Session(engine) as session:
                    computer, credential = provision_agent(session, agent_id)
                    self.assertEqual(computer.authorization_status, "pending")
                    self.assertFalse(is_online(computer.last_seen, 900))
                    self.assertNotIn("credential", Computer.__table__.columns)
                    self.assertIn("agent_credential_hash", Computer.__table__.columns)
                    self.assertTrue(
                        verify_agent_credential(credential, computer.agent_credential_hash)
                    )
                    self.assertNotEqual(computer.agent_credential_hash, credential)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()

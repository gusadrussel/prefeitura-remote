import unittest
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from server.database.models import Computer
from server.database.session import create_database
from server.provision_agent import provision_agent, revoke_agent_credential
from server.services.inventory import is_online
from server.services.agent_credentials import (
    hash_agent_credential,
    verify_agent_credential,
)


class AgentCredentialHashTests(unittest.TestCase):
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

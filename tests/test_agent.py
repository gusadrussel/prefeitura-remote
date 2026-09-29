import tempfile
import unittest
import uuid
from pathlib import Path

from agent.agent import _validate_server_url, get_agent_id


class AgentIdentityAndTransportTests(unittest.TestCase):
    def test_agent_id_is_created_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "agent_id"
            first_id = get_agent_id(path)
            second_id = get_agent_id(path)
            self.assertEqual(first_id, second_id)
            self.assertEqual(str(uuid.UUID(first_id)), first_id)

    def test_transport_requires_tls_outside_loopback(self):
        self.assertEqual(_validate_server_url("https://central.example.gov"), "https://central.example.gov")
        self.assertEqual(_validate_server_url("http://localhost:8000"), "http://localhost:8000")
        for url in ("http://central.example.gov", "ftp://central.example.gov"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _validate_server_url(url)


if __name__ == "__main__":
    unittest.main()
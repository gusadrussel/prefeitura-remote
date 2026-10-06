import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent.agent import _validate_server_url, get_agent_id
from agent.config import load_config


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

    def test_individual_credential_takes_precedence_over_global_legacy_token(self):
        environment = {
            "PREFEITURA_SERVER_URL": "https://central.example.gov",
            "PREFEITURA_AGENT_TOKEN": "legacy-global-value",
            "PREFEITURA_AGENT_CREDENTIAL": "per-agent-random-value",
        }
        with patch.dict("os.environ", environment, clear=True):
            config = load_config()
        self.assertEqual(config.token, "per-agent-random-value")
        self.assertTrue(config.individual_credential)

        with patch.dict(
            "os.environ",
            {key: value for key, value in environment.items() if key != "PREFEITURA_AGENT_CREDENTIAL"},
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.token, "legacy-global-value")
        self.assertFalse(config.individual_credential)


if __name__ == "__main__":
    unittest.main()
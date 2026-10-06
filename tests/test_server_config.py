import os
import unittest
from pathlib import Path
from unittest.mock import patch

from server.config import DEFAULT_DATABASE_URL, PROJECT_ROOT, Settings, load_settings


class ServerDatabaseConfigurationTests(unittest.TestCase):
    def test_default_database_is_in_project_root(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings()

        expected_path = PROJECT_ROOT / "prefeitura_remote.db"
        self.assertEqual(settings.database_url, f"sqlite:///{expected_path}")
        self.assertTrue(expected_path.is_absolute())
        self.assertEqual(DEFAULT_DATABASE_URL, settings.database_url)

    def test_explicit_database_url_overrides_default(self):
        for database_url in (
            "sqlite:////tmp/test-prefeitura.db",
            "postgresql+psycopg://user:pass@localhost:5432/prefeitura",
        ):
            with self.subTest(database_url=database_url):
                with patch.dict(os.environ, {"PREFEITURA_DATABASE_URL": database_url}, clear=True):
                    self.assertEqual(load_settings().database_url, database_url)

    def test_rate_limits_are_configurable_and_must_be_positive(self):
        environment = {
            "PREFEITURA_RATE_LIMIT_WINDOW_SECONDS": "45",
            "PREFEITURA_AUTH_FAILURE_LIMIT": "4",
            "PREFEITURA_INVENTORY_UPDATE_LIMIT": "9",
            "PREFEITURA_VIEWER_TICKET_LIMIT": "8",
            "PREFEITURA_AUTHORIZATION_CHANGE_LIMIT": "6",
            "PREFEITURA_WEBSOCKET_CONNECTION_LIMIT": "12",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = load_settings()
        self.assertEqual(settings.rate_limit_window_seconds, 45)
        self.assertEqual(settings.auth_failures_per_window, 4)
        self.assertEqual(settings.inventory_updates_per_window, 9)
        self.assertEqual(settings.viewer_ticket_requests_per_window, 8)
        self.assertEqual(settings.authorization_changes_per_window, 6)
        self.assertEqual(settings.websocket_connections_per_window, 12)

        with self.assertRaisesRegex(ValueError, "positive integers"):
            Settings(database_url="sqlite://", agent_token="a", dashboard_token="d", auth_failures_per_window=0)

    def test_global_agent_credential_fallback_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(load_settings().allow_legacy_agent_token)
        with patch.dict(
            os.environ,
            {"PREFEITURA_ALLOW_LEGACY_AGENT_TOKEN": "true"},
            clear=True,
        ):
            self.assertTrue(load_settings().allow_legacy_agent_token)

    def test_legacy_agent_token_is_disabled_by_default_and_explicitly_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(load_settings().allow_legacy_agent_token)
        with patch.dict(
            os.environ,
            {"PREFEITURA_ALLOW_LEGACY_AGENT_TOKEN": "true"},
            clear=True,
        ):
            self.assertTrue(load_settings().allow_legacy_agent_token)

    def test_legacy_agent_token_compatibility_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(load_settings().allow_legacy_agent_token)
        with patch.dict(os.environ, {"PREFEITURA_ALLOW_LEGACY_AGENT_TOKEN": "true"}, clear=True):
            self.assertTrue(load_settings().allow_legacy_agent_token)


if __name__ == "__main__":
    unittest.main()
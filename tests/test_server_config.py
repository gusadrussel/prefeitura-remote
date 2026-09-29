import os
import unittest
from pathlib import Path
from unittest.mock import patch

from server.config import DEFAULT_DATABASE_URL, PROJECT_ROOT, load_settings


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


if __name__ == "__main__":
    unittest.main()
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.services.inventory import is_authorized, is_online


class InventoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "api-test.db"
        settings = Settings(
            database_url=f"sqlite:///{database_path}",
            agent_token="agent-test-token",
            dashboard_token="dashboard-test-token",
            online_threshold_seconds=900,
        )
        self.client = TestClient(create_app(settings))

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def test_health_and_dashboard_are_served(self):
        self.assertEqual(self.client.get("/api/v1/health").json(), {"status": "ok"})
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Prefeitura", page.text)
        stylesheet = self.client.get("/dashboard/css/dashboard.css")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn("--forest", stylesheet.text)

    def test_inventory_requires_agent_token_and_can_be_searched(self):
        inventory = {
            "schema_version": 1,
            "agent_id": "a0a0a0a0-1111-4222-8333-123456789abc",
            "agent_version": "0.1.0",
            "system": {
                "hostname": "SEDE-ADM-01",
                "logged_user": "tecnico-local",
                "os_name": "Windows",
                "os_version": "Windows 11",
            },
            "cpu": {"model": "CPU teste", "logical_cores": 8},
        }
        self.assertEqual(
            self.client.post("/api/v1/inventory", json=inventory).status_code, 401
        )
        headers = {"Authorization": "Bearer agent-test-token"}
        response = self.client.post("/api/v1/inventory", json=inventory, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["agent_id"], inventory["agent_id"])
        self.assertEqual(response.json()["authorization_status"], "pending")
        self.assertTrue(response.json()["last_seen"].endswith(("Z", "+00:00")))

        inventory["system"]["hostname"] = "SEDE-ADM-02"
        updated = self.client.post("/api/v1/inventory", json=inventory, headers=headers)
        self.assertEqual(updated.status_code, 201)
        self.assertEqual(updated.json()["computer_id"], response.json()["computer_id"])
        self.assertEqual(updated.json()["authorization_status"], "pending")

        dashboard_headers = {"Authorization": "Bearer dashboard-test-token"}
        listing = self.client.get(
            "/api/v1/computers?search=sede-adm", headers=dashboard_headers
        )
        self.assertEqual(listing.status_code, 200)
        computers = listing.json()
        self.assertEqual(len(computers), 1)
        self.assertEqual(computers[0]["hostname"], "SEDE-ADM-02")
        self.assertTrue(computers[0]["online"])
        self.assertEqual(computers[0]["authorization_status"], "pending")
        self.assertFalse(computers[0]["channel_connected"])

        detail = self.client.get(
            f"/api/v1/computers/{computers[0]['id']}", headers=dashboard_headers
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["inventory"]["cpu"]["model"], "CPU teste")

    def test_admin_authorization_lifecycle_filters_and_preserves_status(self):
        inventory = {
            "agent_id": "b0b0b0b0-1111-4222-8333-123456789abc",
            "system": {"hostname": "SETOR-FIN-01", "os_name": "Linux"},
        }
        agent_headers = {"Authorization": "Bearer agent-test-token"}
        dashboard_headers = {"Authorization": "Bearer dashboard-test-token"}
        created = self.client.post(
            "/api/v1/inventory", json=inventory, headers=agent_headers
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["authorization_status"], "pending")
        computer_id = created.json()["computer_id"]

        self.assertEqual(self.client.get("/api/v1/admin/computers").status_code, 401)
        self.assertEqual(
            self.client.post(f"/api/v1/admin/computers/{computer_id}/authorize").status_code,
            401,
        )
        self.assertEqual(
            self.client.post(f"/api/v1/admin/computers/{computer_id}/revoke").status_code,
            401,
        )
        self.assertEqual(
            self.client.get(f"/api/v1/admin/computers/{computer_id}").status_code,
            401,
        )

        pending = self.client.get(
            "/api/v1/admin/computers?authorization_status=pending",
            headers=dashboard_headers,
        ).json()
        self.assertEqual([computer["id"] for computer in pending], [computer_id])

        authorized = self.client.post(
            f"/api/v1/admin/computers/{computer_id}/authorize",
            headers=dashboard_headers,
        )
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.json()["authorization_status"], "authorized")
        self.assertTrue(is_authorized(SimpleNamespace(authorization_status="authorized")))

        inventory["system"]["hostname"] = "SETOR-FIN-02"
        updated = self.client.post(
            "/api/v1/inventory", json=inventory, headers=agent_headers
        )
        self.assertEqual(updated.json()["computer_id"], computer_id)
        self.assertEqual(updated.json()["authorization_status"], "authorized")
        authorized_filter = self.client.get(
            "/api/v1/computers?authorization_status=authorized",
            headers=dashboard_headers,
        ).json()
        self.assertEqual([computer["id"] for computer in authorized_filter], [computer_id])

        revoked = self.client.post(
            f"/api/v1/admin/computers/{computer_id}/revoke",
            headers=dashboard_headers,
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["authorization_status"], "revoked")
        self.assertFalse(is_authorized(SimpleNamespace(authorization_status="revoked")))

        inventory["system"]["hostname"] = "SETOR-FIN-03"
        after_revoke = self.client.post(
            "/api/v1/inventory", json=inventory, headers=agent_headers
        )
        self.assertEqual(after_revoke.json()["authorization_status"], "revoked")
        revoked_filter = self.client.get(
            "/api/v1/admin/computers?search=SETOR-FIN&authorization_status=revoked",
            headers=dashboard_headers,
        ).json()
        self.assertEqual([computer["id"] for computer in revoked_filter], [computer_id])
        self.assertEqual(
            self.client.get(
                "/api/v1/admin/computers?authorization_status=pending",
                headers=dashboard_headers,
            ).json(),
            [],
        )

        detail = self.client.get(
            f"/api/v1/admin/computers/{computer_id}", headers=dashboard_headers
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["computer"]["authorization_status"], "revoked")

    def test_dashboard_queries_require_a_token(self):
        self.assertEqual(self.client.get("/api/v1/computers").status_code, 401)

    def test_online_status_uses_the_configured_heartbeat_window(self):
        now = datetime(2026, 9, 29, 12, tzinfo=UTC)
        self.assertTrue(is_online(now - timedelta(seconds=899), 900, now))
        self.assertTrue(is_online(now - timedelta(seconds=900), 900, now))
        self.assertFalse(is_online(now - timedelta(seconds=901), 900, now))


if __name__ == "__main__":
    unittest.main()
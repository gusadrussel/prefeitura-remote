import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.provision_agent import provision_agent, revoke_agent_credential
from server.services.inventory import is_authorized, is_online


class InventoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "api-test.db"
        settings = Settings(
            database_url=f"sqlite:///{database_path}",
            agent_token="agent-test-token",
            dashboard_token="dashboard-test-token",
            allow_legacy_agent_token=True,
            online_threshold_seconds=900,
        )
        self.settings = settings
        self.app = create_app(settings)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.app.state.engine.dispose()
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

    def test_authentication_failures_are_limited_and_valid_auth_resets_counter(self):
        self.app.state.settings = replace(
            self.settings,
            auth_failures_per_window=2,
            rate_limit_window_seconds=60,
        )
        for _ in range(2):
            self.assertEqual(self.client.get("/api/v1/admin/computers").status_code, 401)
        limited = self.client.get("/api/v1/admin/computers")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "60")

        dashboard_headers = {"Authorization": "Bearer dashboard-test-token"}
        self.assertEqual(
            self.client.get("/api/v1/admin/computers", headers=dashboard_headers).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/v1/admin/computers").status_code, 401)

    def test_valid_inventory_updates_are_limited_per_agent_without_mutating_on_429(self):
        self.app.state.settings = replace(
            self.settings,
            inventory_updates_per_window=1,
            rate_limit_window_seconds=60,
        )
        inventory = {
            "agent_id": "d1d1d1d1-1111-4222-8333-123456789abc",
            "system": {"hostname": "RATE-LIMIT-01"},
        }
        headers = {"Authorization": "Bearer agent-test-token"}
        first = self.client.post("/api/v1/inventory", json=inventory, headers=headers)
        self.assertEqual(first.status_code, 201)

        inventory["system"]["hostname"] = "RATE-LIMIT-CHANGED"
        limited = self.client.post("/api/v1/inventory", json=inventory, headers=headers)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "60")

        detail = self.client.get(
            f"/api/v1/admin/computers/{first.json()['computer_id']}",
            headers={"Authorization": "Bearer dashboard-test-token"},
        )
        self.assertEqual(detail.json()["computer"]["hostname"], "RATE-LIMIT-01")

    def test_inventory_uses_individual_credential_and_refuses_impersonation_or_revocation(self):
        self.app.state.settings = replace(self.settings, allow_legacy_agent_token=False)
        agent_one = "e1e1e1e1-1111-4222-8333-123456789abc"
        agent_two = "e2e2e2e2-1111-4222-8333-123456789abc"
        session = self.app.state.session_factory()
        try:
            _, credential_one = provision_agent(session, agent_one)
            _, credential_two = provision_agent(session, agent_two)
        finally:
            session.close()

        payload = {"agent_id": agent_one, "system": {"hostname": "INDIVIDUAL-01"}}
        accepted = self.client.post(
            "/api/v1/inventory",
            json=payload,
            headers={"Authorization": f"Bearer {credential_one}"},
        )
        self.assertEqual(accepted.status_code, 201)

        spoofed = self.client.post(
            "/api/v1/inventory",
            json={**payload, "agent_id": agent_two},
            headers={"Authorization": f"Bearer {credential_one}"},
        )
        self.assertEqual(spoofed.status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/v1/inventory",
                json=payload,
                headers={"Authorization": "Bearer agent-test-token"},
            ).status_code,
            401,
        )

        session = self.app.state.session_factory()
        try:
            self.assertTrue(revoke_agent_credential(session, agent_one))
        finally:
            session.close()
        revoked = self.client.post(
            "/api/v1/inventory",
            json=payload,
            headers={"Authorization": f"Bearer {credential_one}"},
        )
        self.assertEqual(revoked.status_code, 401)
        self.assertNotEqual(credential_one, credential_two)

    def test_authorization_change_limit_does_not_apply_the_blocked_change(self):
        self.app.state.settings = replace(
            self.settings,
            authorization_changes_per_window=1,
            rate_limit_window_seconds=60,
        )
        payload = {
            "agent_id": "c1c1c1c1-1111-4222-8333-123456789abc",
            "system": {"hostname": "RATE-LIMIT-TEST"},
        }
        created = self.client.post(
            "/api/v1/inventory",
            json=payload,
            headers={"Authorization": "Bearer agent-test-token"},
        )
        computer_id = created.json()["computer_id"]
        headers = {"Authorization": "Bearer dashboard-test-token"}
        authorized = self.client.post(
            f"/api/v1/admin/computers/{computer_id}/authorize", headers=headers
        )
        self.assertEqual(authorized.status_code, 200)

        rejected = self.client.post(
            f"/api/v1/admin/computers/{computer_id}/revoke", headers=headers
        )
        self.assertEqual(rejected.status_code, 429)
        detail = self.client.get(
            f"/api/v1/admin/computers/{computer_id}", headers=headers
        )
        self.assertEqual(detail.json()["computer"]["authorization_status"], "authorized")

    def test_online_status_uses_the_configured_heartbeat_window(self):
        now = datetime(2026, 9, 29, 12, tzinfo=UTC)
        self.assertTrue(is_online(now - timedelta(seconds=899), 900, now))
        self.assertTrue(is_online(now - timedelta(seconds=900), 900, now))
        self.assertFalse(is_online(now - timedelta(seconds=901), 900, now))


if __name__ == "__main__":
    unittest.main()
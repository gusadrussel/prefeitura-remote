import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy import select

from server.config import Settings
from server.database.models import ActivationCode, Technician
from server.main import create_app
from server.provision_agent import issue_activation_code, provision_agent, revoke_agent_credential
from server.routes.api import _require_admin, create_installation_code, installation_code_policy
from server.services.agent_credentials import hash_agent_credential
from server.services.inventory import is_authorized, is_online


class InventoryApiTests(unittest.TestCase):
    def test_installation_code_issuance_is_admin_only_and_never_lists_codes(self):
        request = SimpleNamespace(app=self.app, client=None)
        with self.app.state.session_factory() as session:
            technician = Technician(username="support", display_name="Support", password_hash="unused",
                                    authorized=True, role="technician", created_at=datetime.now(UTC))
            session.add(technician)
            session.commit()
            token = self.app.state.technician_sessions.issue(technician.id)
            for bearer, status in [(None, 401), (f"Bearer {token}", 403)]:
                with self.assertRaises(HTTPException) as error:
                    _require_admin(request, bearer)
                self.assertEqual(error.exception.status_code, status)
            self.assertIsNone(session.scalar(select(ActivationCode)))
            admin = _require_admin(request, "Bearer dashboard-test-token")
            issued = create_installation_code(request, admin, session)
            data = json.loads(issued.body)
            self.assertEqual(issued.status_code, 201)
            self.assertEqual(issued.headers["Cache-Control"], "no-store")
            self.assertEqual(data["expires_in"], 1800)
            stored = session.scalar(select(ActivationCode))
            self.assertEqual(stored.digest, hash_agent_credential(data["code"]))
            expiry = stored.expires_at.replace(tzinfo=UTC)
            self.assertAlmostEqual((expiry - datetime.now(UTC)).total_seconds(), 1800, delta=5)
            self.assertEqual(json.loads(installation_code_policy(admin).body), {"expires_in": 1800})

    def test_activation_requires_unused_code_and_keeps_approval_and_revocation(self):
        admin = {"Authorization": "Bearer dashboard-test-token"}
        agent_id = str(uuid4())
        headers = {"X-Prefeitura-Activation": "x" * 43, "X-Prefeitura-Agent-Id": agent_id}
        self.assertEqual(self.client.post("/api/v1/agents/activate", headers=headers).status_code, 401)
        with self.app.state.session_factory() as session:
            headers["X-Prefeitura-Activation"] = issue_activation_code(session)
        response = self.client.post("/api/v1/agents/activate", headers=headers)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["agent_id"], agent_id)
        self.assertEqual(self.client.post("/api/v1/agents/activate", headers={
            **headers, "X-Prefeitura-Agent-Id": str(uuid4()),
        }).status_code, 401)
        inventory = {"agent_id": agent_id, "system": {"hostname": "TEST-ACTIVATION"}}
        bearer = {"Authorization": f"Bearer {response.json()['credential']}"}
        self.assertEqual(self.client.post("/api/v1/inventory", json=inventory, headers=bearer).status_code, 201)
        computer, = self.client.get("/api/v1/admin/computers", headers=admin).json()
        self.assertEqual(computer["authorization_status"], "pending")
        path = f"/api/v1/admin/computers/{computer['id']}"
        self.assertEqual(self.client.post(f"{path}/screen/access-request", headers=admin).status_code, 403)
        self.assertEqual(self.client.post(f"{path}/authorize", headers=admin).json()["authorization_status"], "authorized")
        self.assertEqual(self.client.post(f"{path}/screen/access-request", headers=admin).status_code, 409)
        with self.app.state.session_factory() as session:
            self.assertTrue(revoke_agent_credential(session, agent_id))
            another = issue_activation_code(session)
        self.assertEqual(self.client.post("/api/v1/agents/activate", headers={
            **headers, "X-Prefeitura-Activation": another,
        }).status_code, 409)
        self.assertEqual(self.client.post("/api/v1/inventory", json=inventory, headers=bearer).status_code, 401)
        self.assertEqual(self.client.post(f"{path}/screen/access-request", headers=admin).status_code, 403)

    def test_activation_code_has_only_one_concurrent_winner(self):
        with self.app.state.session_factory() as session:
            code = issue_activation_code(session)

        def activate(_):
            return self.client.post("/api/v1/agents/activate", headers={
                "X-Prefeitura-Activation": code, "X-Prefeitura-Agent-Id": str(uuid4()),
            }).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sorted(pool.map(activate, range(4))), [201, 401, 401, 401])

    def test_bootstrap_cannot_rotate_provisioned_or_revoked_credentials(self):
        self.app.state.settings = replace(self.settings, enrollment_token="test-enrollment")
        agent_id = "e4e4e4e4-1111-4222-8333-123456789abc"
        headers = {"X-Prefeitura-Enrollment":"test-enrollment", "X-Prefeitura-Agent-Id":agent_id}
        response = self.client.post("/api/v1/agents/enroll", headers=headers)
        self.assertEqual(response.status_code, 200)
        credential = response.json()["credential"]
        self.assertEqual(self.client.post("/api/v1/agents/enroll",headers=headers).status_code,409)
        payload = {"agent_id":agent_id,"system":{"hostname":"TEST"}}
        self.assertEqual(self.client.post("/api/v1/inventory",json=payload,headers={"Authorization":f"Bearer {credential}"}).status_code,201)
        with self.app.state.session_factory() as session:
            revoke_agent_credential(session, agent_id)
        self.assertEqual(self.client.post("/api/v1/agents/enroll",headers=headers).status_code,409)

    def test_concurrent_enrollment_has_one_winner_and_valid_returned_credential(self):
        self.app.state.settings = replace(self.settings, enrollment_token="test-enrollment")
        agent_id = "e5e5e5e5-1111-4222-8333-123456789abc"
        headers = {"X-Prefeitura-Enrollment":"test-enrollment", "X-Prefeitura-Agent-Id":agent_id}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _:self.client.post("/api/v1/agents/enroll",headers=headers), range(8)))
        self.assertEqual(sorted(response.status_code for response in results), [200] + [409]*7)
        credential = next(response.json()["credential"] for response in results if response.status_code == 200)
        self.assertEqual(self.client.post("/api/v1/inventory",json={"agent_id":agent_id,"system":{"hostname":"TEST"}},headers={"Authorization":f"Bearer {credential}"}).status_code,201)

    def test_enrollment_failure_limit_cannot_be_bypassed_by_changing_agent_id(self):
        self.app.state.settings = replace(self.settings, enrollment_token="test-enrollment", auth_failures_per_window=2)
        for index, expected in enumerate([401,401,429]):
            response = self.client.post("/api/v1/agents/enroll",headers={"X-Prefeitura-Enrollment":"wrong", "X-Prefeitura-Agent-Id":str(index)})
            self.assertEqual(response.status_code, expected)

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
        self.assertIn("<title>Vyntra Console</title>", page.text)
        self.assertEqual(self.client.get("/dashboard/assets/vyntra.svg").status_code, 200)
        stylesheet = self.client.get("/dashboard/css/dashboard.css")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn("--forest", stylesheet.text)

    def test_active_remote_sessions_require_admin_and_follow_acknowledgements(self):
        path = "/api/v1/admin/remote-sessions"
        headers = {"Authorization": "Bearer dashboard-test-token"}
        self.assertEqual(self.client.get(path).status_code, 401)
        account = {"username": "operator", "display_name": "Operator", "password": "test-password-only"}
        self.assertEqual(self.client.post("/api/v1/admin/technicians", json=account, headers=headers).status_code, 200)
        login = self.client.post("/api/v1/auth/login", json={"username": account["username"], "password": account["password"]})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.get(path, headers={"Authorization": "Bearer " + login.json()["token"]}).status_code, 403)

        streams = self.app.state.screen_streams
        viewer = streams.attach("test-agent", 42)
        self.assertTrue(streams.request_start(viewer))
        self.assertEqual(self.client.get(path, headers=headers).json(), [])
        streams.agent_started("test-agent", {"type": "screen.start_ack", "accepted": True})
        response = self.client.get(path, headers=headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json(), [{"computer_id": 42, "mode": "view"}])
        streams.agent_disconnected("test-agent")
        self.assertEqual(self.client.get(path, headers=headers).json(), [])

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

    def test_agent_can_enroll_with_bootstrap_token_and_receive_individual_credential(self):
        from dataclasses import replace
        enrollment_token = "deployment-bootstrap-test-token"
        self.app.state.settings = replace(self.settings, enrollment_token=enrollment_token)
        agent_id = "e1e1e1e1-1111-4222-8333-123456789abc"
        headers = {
            "X-Prefeitura-Enrollment": enrollment_token,
            "X-Prefeitura-Agent-Id": agent_id,
        }
        issued = self.client.post("/api/v1/agents/enroll", headers=headers)
        self.assertEqual(issued.status_code, 200, issued.text)
        credential = issued.json()["credential"]
        self.assertTrue(credential)
        inventory = {"agent_id": agent_id, "system": {"hostname": "AUTO-ENROLL-01"}}
        accepted = self.client.post(
            "/api/v1/inventory",
            json=inventory,
            headers={"Authorization": f"Bearer {credential}"},
        )
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertEqual(accepted.json()["authorization_status"], "pending")

    def test_agent_enrollment_rejects_invalid_bootstrap_token(self):
        from dataclasses import replace
        self.app.state.settings = replace(self.settings, enrollment_token="correct-bootstrap")
        response = self.client.post(
            "/api/v1/agents/enroll",
            headers={
                "X-Prefeitura-Enrollment": "wrong-bootstrap",
                "X-Prefeitura-Agent-Id": "e2e2e2e2-1111-4222-8333-123456789abc",
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_can_provision_individual_agent_credential(self):
        agent_id = "f1f1f1f1-1111-4222-8333-123456789abc"
        inventory = {
            "agent_id": agent_id,
            "system": {"hostname": "PROVISION-01"},
        }
        agent_headers = {"Authorization": "Bearer agent-test-token"}
        dashboard_headers = {"Authorization": "Bearer dashboard-test-token"}

        created = self.client.post(
            "/api/v1/inventory", json=inventory, headers=agent_headers
        )
        self.assertEqual(created.status_code, 201)

        unauthorized = self.client.post(f"/api/v1/admin/agents/{agent_id}/credential")
        self.assertEqual(unauthorized.status_code, 401)

        issued = self.client.post(
            f"/api/v1/admin/agents/{agent_id}/credential",
            headers=dashboard_headers,
        )
        self.assertEqual(issued.status_code, 200)
        data = issued.json()
        self.assertEqual(data["agent_id"], agent_id)
        self.assertTrue(data["credential"])
        self.assertNotIn("agent_credential_hash", data)

        credential = data["credential"]
        self.app.state.settings = replace(self.settings, allow_legacy_agent_token=False)
        accepted = self.client.post(
            "/api/v1/inventory",
            json=inventory,
            headers={"Authorization": f"Bearer {credential}"},
        )
        self.assertEqual(accepted.status_code, 201)

        rotated = self.client.post(
            f"/api/v1/admin/agents/{agent_id}/credential",
            headers=dashboard_headers,
        )
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(rotated.json()["credential"], credential)

        old_credential = self.client.post(
            "/api/v1/inventory",
            json=inventory,
            headers={"Authorization": f"Bearer {credential}"},
        )
        self.assertEqual(old_credential.status_code, 401)

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

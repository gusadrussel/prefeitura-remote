import unittest
from unittest.mock import patch

from server.services.remote_consent import ConsentCapacityError, RemoteConsentRegistry
from server.services.remote_consent import technician_session_valid
from server.services.technician_auth import TechnicianSessionRegistry
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from server.services.screen_streams import ScreenStreamRegistry


class RemoteConsentRegistryTests(unittest.TestCase):
    def test_concurrent_polling_allocates_one_ticket_and_cancellation_blocks_reissue(self):
        registry, streams = RemoteConsentRegistry(), ScreenStreamRegistry()
        request = registry.create("a", 1, "Test")
        registry.respond(request.request_id, "a", 1, True)
        barrier = Barrier(12)
        def issue(_):
            barrier.wait(timeout=5)
            return registry.issue_view_ticket(request.request_id, streams)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(issue, range(12)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(streams._tickets), 1)
        registry.cancel_for_computer(1, "revoked")
        self.assertIsNone(registry.issue_view_ticket(request.request_id, streams))
        self.assertIsNone(registry.for_ticket(results[0][0], 1))

    def test_expired_login_and_logout_invalidate_only_bound_consent(self):
        sessions = TechnicianSessionRegistry(ttl_seconds=10)
        app = SimpleNamespace(state=SimpleNamespace(technician_sessions=sessions))
        registry = RemoteConsentRegistry()
        with patch("server.services.technician_auth.time.monotonic", return_value=0):
            token = sessions.issue(1)
            request = registry.create("a",1,"Test", principal="tech",session_token=token)
            registry.respond(request.request_id,"a",1,True)
            self.assertTrue(technician_session_valid(app,request))
        with patch("server.services.technician_auth.time.monotonic", return_value=11):
            self.assertFalse(technician_session_valid(app,request))
            other = registry.create("b",2,"Test",principal="other",session_token="other-token")
            self.assertEqual(registry.cancel_session(token), {"a"})
            self.assertEqual(other.status,"pending")

    def test_control_requires_active_parent_and_cannot_be_replayed(self):
        registry = RemoteConsentRegistry()
        view = registry.create("a", 1, "Test", principal="tech")
        registry.respond(view.request_id, "a", 1, True)
        control = registry.create("a", 1, "Test", mode="control", principal="tech", parent_id=view.request_id)
        registry.respond(control.request_id, "a", 1, True)
        self.assertIsNone(registry.claim_control(view.request_id))
        self.assertTrue(registry.claim(view.request_id, "view"))
        self.assertEqual(registry.claim_control(view.request_id), control.request_id)
        self.assertIsNone(registry.claim_control(view.request_id))
        registry.end(view.request_id, "user_stopped")
        self.assertFalse(registry.active(control.request_id, "control"))
        self.assertFalse(registry.active(view.request_id, "view"))

    def test_cancel_revokes_approved_tickets_and_bounded_store_reclaims_them(self):
        registry = RemoteConsentRegistry()
        with patch("server.services.remote_consent.time.monotonic", return_value=10):
            request = registry.create("a", 1, "Test", ttl_seconds=1)
            registry.respond(request.request_id, "a", 1, True)
            registry.attach_ticket(request.request_id, "fake-ticket", 30)
            self.assertIsNotNone(registry.for_ticket("fake-ticket", 1))
            registry.cancel_for_computer(1, "revoked")
            self.assertIsNone(registry.for_ticket("fake-ticket", 1))
        with patch("server.services.remote_consent.time.monotonic", return_value=400):
            registry.create("b", 2, "Test")
            self.assertNotIn(request.request_id, registry._requests)

    def test_request_stays_pending_until_bound_agent_responds(self):
        registry = RemoteConsentRegistry()
        request = registry.create("agent-1", 7, "Técnico da TI", ttl_seconds=30)
        self.assertEqual(request.status, "pending")
        self.assertIsNone(registry.respond(request.request_id, "agent-2", 7, True))
        self.assertEqual(registry.get(request.request_id).status, "pending")
        approved = registry.respond(request.request_id, "agent-1", 7, True)
        self.assertEqual(approved.status, "approved")

    def test_rejected_request_cannot_be_approved_again(self):
        registry = RemoteConsentRegistry()
        request = registry.create("agent-1", 7, "Técnico da TI")
        rejected = registry.respond(request.request_id, "agent-1", 7, False)
        self.assertEqual(rejected.status, "rejected")
        again = registry.respond(request.request_id, "agent-1", 7, True)
        self.assertIsNone(again)

    def test_expired_request_is_not_approved(self):
        registry = RemoteConsentRegistry()
        with patch("server.services.remote_consent.time.monotonic") as monotonic:
            monotonic.return_value = 10
            request = registry.create("agent-1", 7, "Técnico da TI", ttl_seconds=1)
            monotonic.return_value = 12
            expired = registry.respond(request.request_id, "agent-1", 7, True)
            self.assertIsNone(expired)
            self.assertEqual(registry.get(request.request_id).status, "expired")

    def test_capacity_does_not_evict_pending_request(self):
        registry = RemoteConsentRegistry()
        with patch("server.services.remote_consent.MAX_PENDING_CONSENT_REQUESTS", 1):
            first = registry.create("agent-1", 7, "Técnico da TI")
            with self.assertRaises(ConsentCapacityError):
                registry.create("agent-2", 8, "Técnico da TI")
            self.assertEqual(registry.get(first.request_id).status, "pending")


if __name__ == "__main__":
    unittest.main()

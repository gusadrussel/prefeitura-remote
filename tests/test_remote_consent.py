import unittest
from unittest.mock import patch

from server.services.remote_consent import ConsentCapacityError, RemoteConsentRegistry


class RemoteConsentRegistryTests(unittest.TestCase):
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
        self.assertEqual(again.status, "rejected")

    def test_expired_request_is_not_approved(self):
        registry = RemoteConsentRegistry()
        with patch("server.services.remote_consent.time.monotonic") as monotonic:
            monotonic.return_value = 10
            request = registry.create("agent-1", 7, "Técnico da TI", ttl_seconds=1)
            monotonic.return_value = 12
            expired = registry.respond(request.request_id, "agent-1", 7, True)
            self.assertEqual(expired.status, "expired")

    def test_capacity_does_not_evict_pending_request(self):
        registry = RemoteConsentRegistry()
        with patch("server.services.remote_consent.MAX_PENDING_CONSENT_REQUESTS", 1):
            first = registry.create("agent-1", 7, "Técnico da TI")
            with self.assertRaises(ConsentCapacityError):
                registry.create("agent-2", 8, "Técnico da TI")
            self.assertEqual(registry.get(first.request_id).status, "pending")


if __name__ == "__main__":
    unittest.main()

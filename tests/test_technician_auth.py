import unittest
from unittest.mock import patch

from server.services.technician_auth import TechnicianSessionRegistry, hash_password, verify_password
from server.services.technician_auth import SessionCapacityError


class TechnicianAuthTests(unittest.TestCase):
    def test_session_store_is_bounded_and_reclaims_expired_sessions(self):
        registry = TechnicianSessionRegistry(ttl_seconds=10, max_sessions=1)
        with patch("server.services.technician_auth.time.monotonic", return_value=0):
            first = registry.issue(1)
            with self.assertRaises(SessionCapacityError):
                registry.issue(2)
            self.assertEqual(registry.get(first), 1)
        with patch("server.services.technician_auth.time.monotonic", return_value=11):
            second = registry.issue(2)
            self.assertIsNone(registry.get(first))
            self.assertEqual(registry.get(second), 2)

    def test_password_hashing_and_verification(self):
        encoded = hash_password("senha-super-segura")
        self.assertTrue(verify_password("senha-super-segura", encoded))
        self.assertFalse(verify_password("senha-errada", encoded))

    def test_session_expires(self):
        registry = TechnicianSessionRegistry(ttl_seconds=10)
        with patch("server.services.technician_auth.time.monotonic", side_effect=[0, 11]):
            token = registry.issue(7)
            self.assertIsNone(registry.get(token))

    def test_revoke_technician_invalidates_all_sessions(self):
        registry = TechnicianSessionRegistry()
        first = registry.issue(7)
        second = registry.issue(7)
        other = registry.issue(8)
        registry.revoke_technician(7)
        self.assertIsNone(registry.get(first))
        self.assertIsNone(registry.get(second))
        self.assertEqual(registry.get(other), 8)


if __name__ == "__main__":
    unittest.main()

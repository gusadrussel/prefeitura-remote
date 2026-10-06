import unittest

from server.services.rate_limits import InMemoryRateLimiter


class InMemoryRateLimiterTests(unittest.TestCase):
    def test_allows_limit_then_blocks_until_window_renews(self):
        limiter = InMemoryRateLimiter()

        self.assertTrue(limiter.allow("auth:192.0.2.1", limit=2, window_seconds=60, now=100))
        self.assertTrue(limiter.allow("auth:192.0.2.1", limit=2, window_seconds=60, now=101))
        self.assertFalse(limiter.allow("auth:192.0.2.1", limit=2, window_seconds=60, now=102))
        self.assertTrue(limiter.allow("auth:192.0.2.1", limit=2, window_seconds=60, now=160))

    def test_keys_have_independent_windows_and_success_can_reset_a_bucket(self):
        limiter = InMemoryRateLimiter()
        self.assertTrue(limiter.allow("auth:192.0.2.1", limit=1, window_seconds=60, now=10))
        self.assertFalse(limiter.allow("auth:192.0.2.1", limit=1, window_seconds=60, now=11))
        self.assertTrue(limiter.allow("auth:192.0.2.2", limit=1, window_seconds=60, now=11))

        limiter.reset("auth:192.0.2.1")
        self.assertTrue(limiter.allow("auth:192.0.2.1", limit=1, window_seconds=60, now=12))

    def test_bounded_key_store_reclaims_expired_keys_and_fails_closed_when_full(self):
        limiter = InMemoryRateLimiter(max_keys=1)
        self.assertTrue(limiter.allow("source-a", limit=2, window_seconds=10, now=0))
        self.assertFalse(limiter.allow("source-b", limit=2, window_seconds=10, now=1))
        self.assertTrue(limiter.allow("source-b", limit=2, window_seconds=10, now=10))

    def test_rejects_non_positive_configuration(self):
        limiter = InMemoryRateLimiter()
        with self.assertRaises(ValueError):
            limiter.allow("source", limit=0, window_seconds=60, now=0)
        with self.assertRaises(ValueError):
            limiter.allow("source", limit=1, window_seconds=0, now=0)

    def test_limited_check_does_not_consume_attempts(self):
        limiter = InMemoryRateLimiter()
        self.assertFalse(limiter.is_limited("auth:source", limit=1, window_seconds=60, now=0))
        self.assertTrue(limiter.allow("auth:source", limit=1, window_seconds=60, now=0))
        self.assertTrue(limiter.is_limited("auth:source", limit=1, window_seconds=60, now=1))
        self.assertFalse(limiter.is_limited("auth:source", limit=1, window_seconds=60, now=60))


if __name__ == "__main__":
    unittest.main()

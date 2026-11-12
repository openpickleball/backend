"""The test run uses an in-memory cache, not the Valkey server.

Settings point the default cache at REDIS_URL. The test runner swaps it
for LocMemCache so the suite needs no cache server.
"""

from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.test import SimpleTestCase


class TestRunCacheTests(SimpleTestCase):
    def test_default_cache_is_in_memory(self) -> None:
        self.assertIsInstance(caches["default"], LocMemCache)

    def test_round_trip(self) -> None:
        cache = caches["default"]
        cache.set("test-run-key", "ok")
        self.assertEqual(cache.get("test-run-key"), "ok")

"""Every throttle counter gets an expiry (issue #34 review).

Django's Redis cache runs ``incr`` as EXISTS then INCR. If the key is
evicted between the two, INCR creates it again at 1 with no expiry, and
a key holding an Account id or a client IP would live forever. So a
count of 1 always sets the counter's expiry again with ``touch``.
Made-up data only.
"""

from types import SimpleNamespace
from typing import Any

from django.test import override_settings

from config.throttling import AccountThrottle

DAY = 86_400
START = 20_833 * DAY + 8 * 3_600


class _EvictingCache:
    """A cache whose key is evicted between ``add`` and ``incr``.

    ``incr`` then creates the key at 1 with no expiry, as Redis INCR
    does on a missing key.
    """

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.adds: list[tuple[str, int]] = []
        self.touches: list[tuple[str, int]] = []

    def add(self, key: str, value: int, timeout: int) -> bool:
        self.adds.append((key, timeout))
        return True

    def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def touch(self, key: str, timeout: int) -> bool:
        self.touches.append((key, timeout))
        return True


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=5, API_ACCOUNT_THROTTLE_PER_DAY=50)
def test_a_new_counter_always_gets_its_expiry() -> None:
    cache = _EvictingCache()
    throttle = AccountThrottle("expiry-test")
    throttle.cache = cache  # type: ignore[assignment]
    throttle.timer = lambda: float(START)

    assert throttle.allow_request(SimpleNamespace(auth=SimpleNamespace(pk=8_101)))  # type: ignore[arg-type]

    assert cache.touches == cache.adds
    assert len(cache.touches) == 2


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=5, API_ACCOUNT_THROTTLE_PER_DAY=50)
def test_a_counter_past_one_is_not_touched() -> None:
    cache = _EvictingCache()
    throttle = AccountThrottle("expiry-test-2")
    throttle.cache = cache  # type: ignore[assignment]
    throttle.timer = lambda: float(START)
    request: Any = SimpleNamespace(auth=SimpleNamespace(pk=8_201))
    throttle.allow_request(request)
    cache.touches.clear()

    assert throttle.allow_request(request)

    assert cache.touches == []

"""Fixtures for the config tests.

The API throttle tests that count requests through Django expect a run
of requests to fall in one minute window. With the real clock, a minute
edge between two requests would start a new window and let a request
that should be refused through. For those modules only, every API
throttle reads a fixed time instead, in the middle of a minute and far
from a UTC day edge. A test that sets a throttle's ``timer`` itself still
wins, since an instance or subclass attribute comes first.
"""

import pytest

from config.throttling import FixedWindowThrottle

DAY = 86_400
# Eight hours and thirty seconds into a UTC day.
PINNED_NOW = float(20_833 * DAY + 8 * 3_600 + 30)

_PINNED_MODULES = frozenset(
    {
        "config.tests.test_api_throttling",
        "config.tests.test_client_ip_throttle",
    }
)


@pytest.fixture(autouse=True)
def _pin_throttle_clock(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.module.__name__ in _PINNED_MODULES:
        monkeypatch.setattr(
            FixedWindowThrottle, "timer", staticmethod(lambda: PINNED_NOW)
        )

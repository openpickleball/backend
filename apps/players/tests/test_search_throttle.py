"""The player search throttle counts correctly (issue #191 review).

A request the minute limit refuses must not use up the day limit, every
request must be counted even when two from one Account interleave, and a
throttle shared by every request must keep no request's state on itself.
Made-up data only.
"""

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.players.api import router

pytestmark = pytest.mark.django_db

SEARCH = "/api/v1/players/search"
MINUTE_LIMIT = 30
DAY_LIMIT = 300
DAY = 86_400
# Eight hours into a UTC day, on a minute boundary: far from either edge.
START = 20_833 * DAY + 8 * 3_600


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def _throttles() -> list[Any]:
    """The throttle objects the served search operation checks."""
    (operation,) = router.path_operations["/search"].operations
    return list(operation.throttle_objects)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every search throttle's clock, moved by hand: ``clock[0]`` is now."""
    now = [float(START)]
    for throttle in _throttles():
        monkeypatch.setattr(throttle, "timer", lambda: now[0], raising=False)
    return now


def _allowed(request: Any) -> bool:
    """Run the throttles as ninja does: every one is asked."""
    results = [throttle.allow_request(request) for throttle in _throttles()]
    return all(results)


def _request(pk: int) -> Any:
    return SimpleNamespace(auth=SimpleNamespace(pk=pk), META={})


def test_minute_refused_burst_does_not_use_up_the_day_limit(
    clock: list[float],
) -> None:
    client = Client()
    client.force_login(UserFactory.create())
    for _ in range(MINUTE_LIMIT):
        assert client.get(SEARCH, {"q": "sam"}).status_code == 200
    for _ in range(DAY_LIMIT - MINUTE_LIMIT):
        assert client.get(SEARCH, {"q": "sam"}).status_code == 429

    clock[0] += 61

    assert client.get(SEARCH, {"q": "sam"}).status_code == 200


class _Interleaving:
    """A cache proxy that runs another request inside the first cache call.

    The nested request runs after the real call returns and before its
    result reaches the throttle, as a request on another worker would.
    """

    def __init__(self, real: Any, state: dict[str, Any]) -> None:
        self._real = real
        self._state = state

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._real, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attribute(*args, **kwargs)
            if not self._state["done"]:
                self._state["done"] = True
                nested: Callable[[], bool] = self._state["nested"]
                assert nested()
            return result

        return call


def test_interleaved_requests_are_each_counted(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(4_101)
    state: dict[str, Any] = {"done": False, "nested": lambda: _allowed(request)}
    for throttle in _throttles():
        monkeypatch.setattr(throttle, "cache", _Interleaving(throttle.cache, state))

    assert _allowed(request)
    assert state["done"]
    monkeypatch.undo()
    for throttle in _throttles():
        monkeypatch.setattr(throttle, "timer", lambda: clock[0], raising=False)

    # Two counted so far: 28 more fill the minute, and the next is refused.
    for _ in range(MINUTE_LIMIT - 2):
        assert _allowed(request)
    assert not _allowed(request)


def test_allow_request_keeps_no_request_state_on_the_throttle(
    clock: list[float],
) -> None:
    before = [dict(vars(throttle)) for throttle in _throttles()]

    for pk in (4_201, 4_202):
        _allowed(_request(pk))

    assert [dict(vars(throttle)) for throttle in _throttles()] == before

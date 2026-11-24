"""Retry-After once the day window is used up (issue #34 review).

After the day limit is reached, requests still count in the minute
window, so the minute window can be the first to refuse. The request
stays refused until the day ends, so ``wait()`` must say so: the latest
end of the refusing window and every later window that is already full.
Later windows are only read, never counted, so a burst the minute
refuses still does not use up the day. Made-up data only.
"""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.players.api import SearchThrottle
from config.throttling import AccountThrottle

DAY = 86_400
START = 20_833 * DAY + 8 * 3_600


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def _request(pk: int) -> Any:
    return SimpleNamespace(auth=SimpleNamespace(pk=pk), META={})


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=3)
def test_minute_refusal_after_the_day_is_spent_waits_for_the_day() -> None:
    now = [float(START)]
    throttle = AccountThrottle("spent-day-test")
    throttle.timer = lambda: now[0]
    request = _request(7_101)
    assert throttle.allow_request(request)
    assert throttle.allow_request(request)
    now[0] += 60
    assert throttle.allow_request(request)
    # The day is spent: the day window refuses this one.
    assert not throttle.allow_request(request)
    assert throttle.wait() == pytest.approx(DAY - 8 * 3_600 - 60)

    # The minute window now holds 3 and refuses first.
    assert not throttle.allow_request(request)

    assert throttle.wait() == pytest.approx(DAY - 8 * 3_600 - 60)


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=3)
def test_minute_refusal_reads_the_day_without_counting_it() -> None:
    now = [float(START)]
    throttle = AccountThrottle("spent-day-count-test")
    throttle.timer = lambda: now[0]
    request = _request(7_201)
    for _ in range(2):
        assert throttle.allow_request(request)
    for _ in range(5):
        assert not throttle.allow_request(request)
        assert throttle.wait() == pytest.approx(60)

    now[0] += 60

    # The day counted only the two allowed requests.
    assert throttle.allow_request(request)
    assert not throttle.allow_request(request)


def test_search_minute_refusal_after_its_day_is_spent_waits_for_the_day() -> None:
    now = [float(START)]
    throttle = SearchThrottle("spent-day-search-test")
    throttle.timer = lambda: now[0]
    request = _request(7_301)
    for _ in range(10):
        for _ in range(30):
            assert throttle.allow_request(request)
        now[0] += 60
    assert not throttle.allow_request(request)
    for _ in range(30):
        throttle.allow_request(request)

    assert not throttle.allow_request(request)

    assert throttle.wait() == pytest.approx(DAY - 8 * 3_600 - 600)

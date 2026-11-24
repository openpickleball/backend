"""The day window refuses a signed-in request through the API (issue #34).

With a high minute limit and a low day limit, the day window is the one
that refuses, and ``Retry-After`` is the rest of the UTC day. The clock is
pinned, so the expected seconds are exact. Made-up data only.
"""

from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from apps.accounts.tests.factories import UserFactory
from config.throttling import FixedWindowThrottle

pytestmark = pytest.mark.django_db

ME = "/api/v1/accounts/me"
DAY = 86_400
# Eight hours and thirty seconds into a UTC day.
NOW = float(20_833 * DAY + 8 * 3_600 + 30)
REST_OF_DAY = DAY - 8 * 3_600 - 30


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(FixedWindowThrottle, "timer", staticmethod(lambda: NOW))
    cache.clear()
    yield
    cache.clear()


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=100, API_ACCOUNT_THROTTLE_PER_DAY=2)
def test_day_refusal_retry_after_is_the_rest_of_the_utc_day() -> None:
    client = Client()
    client.force_login(UserFactory.create())
    assert [client.get(ME).status_code for _ in range(2)] == [200, 200]

    response = client.get(ME)

    assert response.status_code == 429
    assert response.json()["code"] == "throttled"
    assert int(response["Retry-After"]) == REST_OF_DAY
    assert REST_OF_DAY > 60

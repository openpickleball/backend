"""A cross-origin client can read Retry-After (issue #34 review).

``Retry-After`` is not a CORS-safelisted response header, so a browser
hides it from a script on another origin unless the response names it in
``Access-Control-Expose-Headers``. Made-up data only.
"""

from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from apps.accounts.tests.factories import UserFactory
from config.throttling import FixedWindowThrottle

pytestmark = pytest.mark.django_db

ME = "/api/v1/accounts/me"
ORIGIN = "https://app.example.org"
DAY = 86_400
NOW = float(20_833 * DAY + 8 * 3_600 + 30)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(FixedWindowThrottle, "timer", staticmethod(lambda: NOW))
    cache.clear()
    yield
    cache.clear()


def _exposed(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


@override_settings(
    CORS_ALLOWED_ORIGINS=[ORIGIN],
    API_ACCOUNT_THROTTLE_PER_MINUTE=1,
    API_ACCOUNT_THROTTLE_PER_DAY=100,
)
def test_throttled_response_exposes_retry_after_cross_origin() -> None:
    client = Client()
    client.force_login(UserFactory.create())
    assert client.get(ME, headers={"origin": ORIGIN}).status_code == 200

    response = client.get(ME, headers={"origin": ORIGIN})

    assert response.status_code == 429
    assert response["Access-Control-Allow-Origin"] == ORIGIN
    assert "retry-after" in _exposed(response["Access-Control-Expose-Headers"])
    assert response["Retry-After"] == "30"

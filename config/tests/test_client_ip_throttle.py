"""The client IP throttle on a public operation, through Django (issue #34).

No public operation is mounted yet (#53 adds the first), so these tests
mount test-only operations on their own API and URLconf. Requests go
through Django's test client and the full middleware, so headers reach
the throttle as a WSGI server passes them. Made-up addresses only.
"""

import uuid
from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.http import HttpRequest
from django.test import Client, override_settings
from django.urls import path

from config.api import OpenNinjaAPI
from config.auth import account_auth
from config.throttling import ClientIPThrottle

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

PUBLIC = "/ip-test/public"
SIGNED_IN = "/ip-test/signed-in"

_api = OpenNinjaAPI(urls_namespace=f"test-ip-{uuid.uuid4().hex}")


@_api.get(
    "/public", auth=None, throttle=ClientIPThrottle("ip_test"), operation_id="ip_public"
)
def ip_public(request: HttpRequest) -> dict[str, bool]:
    return {"ok": True}


@_api.get(
    "/signed-in",
    auth=account_auth,
    throttle=ClientIPThrottle("ip_test_signed_in"),
    operation_id="ip_signed_in",
)
def ip_signed_in(request: HttpRequest) -> dict[str, bool]:
    return {"ok": True}


urlpatterns = [path("ip-test/", _api.urls)]

LIMITS = override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=2, API_CLIENT_IP_THROTTLE_PER_DAY=100
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    with LIMITS:
        yield
    cache.clear()


def _get(url: str = PUBLIC, **headers: str) -> int:
    response = Client().get(url, headers=headers)
    return response.status_code


def test_two_client_ips_have_separate_limits() -> None:
    first = {"do-connecting-ip": "203.0.113.7"}
    assert [_get(**first) for _ in range(3)] == [200, 200, 429]

    assert _get(**{"do-connecting-ip": "203.0.113.8"}) == 200


def test_forged_forwarded_for_with_the_same_ip_shares_a_limit() -> None:
    statuses = [
        _get(**{"do-connecting-ip": "203.0.113.7", "x-forwarded-for": forged})
        for forged in ("198.51.100.1", "198.51.100.2", "198.51.100.3")
    ]

    assert statuses == [200, 200, 429]


def test_forged_forwarded_for_without_the_header_shares_the_fallback() -> None:
    statuses = [
        _get(**{"x-forwarded-for": forged})
        for forged in ("198.51.100.1", "198.51.100.2", "198.51.100.3")
    ]

    assert statuses == [200, 200, 429]


def test_remote_addr_is_not_a_client_ip() -> None:
    statuses = [
        Client(REMOTE_ADDR=address).get(PUBLIC).status_code
        for address in ("198.51.100.1", "198.51.100.2", "198.51.100.3")
    ]

    assert statuses == [200, 200, 429]


def test_configured_header_is_the_one_read() -> None:
    with override_settings(CLIENT_IP_HEADER="x-test-client-ip"):
        assert [
            _get(**{"x-test-client-ip": "203.0.113.7", "do-connecting-ip": ip})
            for ip in ("203.0.113.20", "203.0.113.21", "203.0.113.22")
        ] == [200, 200, 429]

        assert _get(**{"x-test-client-ip": "203.0.113.8"}) == 200


def test_refusal_is_a_throttled_error_with_retry_after() -> None:
    for _ in range(2):
        _get(**{"do-connecting-ip": "203.0.113.7"})

    response = Client().get(PUBLIC, headers={"do-connecting-ip": "203.0.113.7"})

    assert response.status_code == 429
    assert response.json() == {
        "code": "throttled",
        "message": "Too many requests.",
        "errors": [],
    }
    retry_after = response["Retry-After"]
    assert retry_after.isdigit()
    assert 1 <= int(retry_after) <= 60


def test_unauthenticated_requests_are_not_counted() -> None:
    headers = {"do-connecting-ip": "203.0.113.7"}
    assert [_get(SIGNED_IN, **headers) for _ in range(5)] == [401] * 5

    # The IP's budget on the public operation is a separate scope.
    assert [_get(**headers) for _ in range(3)] == [200, 200, 429]
    keys = [str(key) for key in cache._cache]  # type: ignore[attr-defined]
    assert not any("ip_test_signed_in" in key for key in keys)

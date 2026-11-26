"""allauth's rate limits, from OPEN's settings and OPEN's client IP (#100).

allauth's per-IP limits read the client IP from the account adapter's
``get_client_ip``, which OPEN takes from ``CLIENT_IP_HEADER`` only,
parsed as the API throttles parse it (ADR 0030). A failed login limit
answers 429. Made-up data only.
"""

from typing import TYPE_CHECKING

import pytest
from allauth.account import app_settings as account_settings
from allauth.account.models import EmailAddress
from django.conf import settings
from django.test import Client, RequestFactory

from apps.accounts.adapter import AccountAdapter
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession, post

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

EMAIL = "limits.player@example.org"
IP_HEADER = settings.CLIENT_IP_HEADER

LIMITED_ACTIONS = (
    "login",
    "login_failed",
    "signup",
    "reset_password",
    "reset_password_from_key",
    "confirm_email",
    "reauthenticate",
    "change_password",
    "manage_email",
)


@pytest.fixture
def account() -> "_User":
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    return account


def _ip(**meta: str) -> str:
    request = RequestFactory().get("/")
    request.META.pop("REMOTE_ADDR", None)
    request.META.update(meta)
    return AccountAdapter(request).get_client_ip(request)


def _header(value: str) -> dict[str, str]:
    return {"HTTP_" + IP_HEADER.upper().replace("-", "_"): value}


@pytest.mark.parametrize("action", LIMITED_ACTIONS)
def test_settings_set_each_limit(action: str) -> None:
    assert settings.ACCOUNT_RATE_LIMITS[action]
    assert account_settings.RATE_LIMITS[action] == settings.ACCOUNT_RATE_LIMITS[action]


def test_the_failed_login_limit_counts_per_ip_and_per_account() -> None:
    pers = {
        rate.rsplit("/", 1)[-1]
        for rate in settings.ACCOUNT_RATE_LIMITS["login_failed"].split(",")
    }

    assert pers == {"ip", "key"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("203.0.113.7", "203.0.113.7"),
        (" 203.0.113.7 ", "203.0.113.7"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::"),
        ("not an address", "0.0.0.0"),
        ("203.0.113.7, 198.51.100.1", "0.0.0.0"),
    ],
)
def test_the_client_ip_comes_from_the_proxy_header(value: str, expected: str) -> None:
    assert _ip(**_header(value)) == expected


def test_forgeable_headers_and_the_peer_address_are_never_read() -> None:
    assert (
        _ip(
            HTTP_X_FORWARDED_FOR="203.0.113.9",
            HTTP_FORWARDED="for=203.0.113.9",
            REMOTE_ADDR="203.0.113.9",
        )
        == "0.0.0.0"
    )


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_repeated_failed_logins_for_one_account_get_429(
    base: str, account: "_User"
) -> None:
    statuses = []
    for attempt in range(12):
        session = HeadlessSession(base)
        response = session.client.post(
            f"{base}/auth/login",
            {"email": EMAIL, "password": "not-the-password"},
            content_type="application/json",
            headers={
                **session._headers(),
                IP_HEADER: f"198.51.100.{attempt + 1}",
                "X-Forwarded-For": f"203.0.113.{attempt + 1}",
                "Forwarded": f"for=203.0.113.{attempt + 1}",
            },
            REMOTE_ADDR=f"192.0.2.{attempt + 1}",
        )
        statuses.append(response.status_code)

    assert 429 in statuses
    first = statuses.index(429)
    assert set(statuses[:first]) == {400}
    assert set(statuses[first:]) == {429}
    # The right password is refused while the Account is locked.
    session = HeadlessSession(base)
    response = session.client.post(
        f"{base}/auth/login",
        {"email": EMAIL, "password": PASSWORD},
        content_type="application/json",
        headers={**session._headers(), IP_HEADER: "198.51.100.99"},
    )
    assert response.status_code == 429


def test_the_per_ip_failed_login_limit_keys_on_the_proxy_header() -> None:
    def fail(email: str, ip: str) -> int:
        return int(
            post(
                Client(),
                f"{APP}/auth/login",
                {"email": email, "password": "not-the-password"},
                **{IP_HEADER: ip, "X-Forwarded-For": f"203.0.113.{len(email)}"},
            ).status_code
        )

    statuses = [fail(f"guess{n}@example.org", "198.51.100.5") for n in range(12)]

    assert statuses[-1] == 429
    # Another client IP still gets an answer about the password.
    assert fail("other@example.org", "198.51.100.6") == 400

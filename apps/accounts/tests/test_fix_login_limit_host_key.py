"""The failed login limit per Account ignores the request's host (#100).

allauth keys its per-account failed login counter on the current site's
domain and the address. With no sites app, the domain is the request's
Host header as sent: an allowed host in another letter case, or with any
port, passes Django's host check and starts a new counter, so a guesser
could reset the limit at will. OPEN keys the counter on the address alone.
Made-up data only.
"""

import pytest
from allauth.account.models import EmailAddress
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, PASSWORD, post

pytestmark = pytest.mark.django_db

EMAIL = "host.key@example.org"


def test_a_changed_host_does_not_reset_the_failed_login_limit() -> None:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    statuses = []
    for attempt in range(8):
        response = post(
            Client(),
            f"{APP}/auth/login",
            {"email": EMAIL, "password": "not-the-password"},
            # A new port, and a new IP, on every attempt.
            HOST=f"testserver:{8000 + attempt}",
            **{"do-connecting-ip": f"198.51.100.{attempt + 1}"},
        )
        statuses.append(response.status_code)

    response = post(
        Client(),
        f"{APP}/auth/login",
        {"email": EMAIL, "password": PASSWORD},
        HOST="TESTSERVER:9999",
        **{"do-connecting-ip": "198.51.100.200"},
    )

    # The right password is refused too once the Account is locked.
    assert response.status_code != 200, statuses

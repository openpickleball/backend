"""A reset from a key answers signed out when it ends the session that
made it (#100 review).

A session signed in to the Account resets its password: the reset ends
every session, that one included, so the answer must not say it is
still authenticated. Made-up data only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    NEW_PASSWORD,
    PASSWORD,
    RESET_PREFIX,
    HeadlessSession,
    key_from_link,
    link_in_last_mail,
)

pytestmark = pytest.mark.django_db

EMAIL = "reset.signed.in@example.org"


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_signed_in_reset_answers_signed_out(base: str) -> None:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    session = HeadlessSession(base)
    assert session.login(EMAIL).status_code == 200
    assert session.post("auth/password/request", {"email": EMAIL}).status_code == 200
    key = key_from_link(link_in_last_mail(RESET_PREFIX), RESET_PREFIX)

    response = session.post(
        "auth/password/reset", {"key": key, "password": NEW_PASSWORD}
    )

    body: dict[str, Any] = response.json()
    assert response.status_code == 401, body
    assert body["meta"]["is_authenticated"] is False
    assert session.get("auth/session").status_code in (401, 410)

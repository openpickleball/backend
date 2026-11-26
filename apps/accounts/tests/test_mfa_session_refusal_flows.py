"""The MFA management views' refusal offers only the MFA reauthentication
(ADR 0043): a password reauthentication does not satisfy it. Made-up data
only."""

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    BROWSER,
    PASSWORD,
    HeadlessSession,
    browser_client,
)

pytestmark = pytest.mark.django_db


def test_the_refusal_lists_only_the_mfa_reauthentication_flow() -> None:
    staff = UserFactory.create(password=PASSWORD, is_staff=True)
    EmailAddress.objects.create(
        user=staff, email=staff.email, verified=True, primary=True
    )
    session = HeadlessSession(BROWSER)
    session.client = browser_client()
    # A password-only session (Django's own login).
    assert session.client.login(username=staff.email, password=PASSWORD)
    session.reauthenticate()

    response = session.delete("account/authenticators/totp")

    assert response.status_code == 401
    assert [flow["id"] for flow in response.json()["data"]["flows"]] == [
        "mfa_reauthenticate"
    ]

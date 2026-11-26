"""Logging out of the admin works after its session limit (issue #101).

A staff session whose latest MFA step is older than
``ADMIN_SESSION_SECONDS``, a password-only staff session, and a staff
Account without MFA no longer open the admin, but logging out must still
end the session. Made-up data only.
"""

from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.admin import age_mfa_steps, staff_with_totp, verified_account
from apps.accounts.tests.headless import PASSWORD

pytestmark = pytest.mark.django_db

ME = "/api/v1/accounts/me"


def _logged_out(client: Client) -> None:
    response = client.post(reverse("admin:logout"))

    assert response.status_code == 200
    assert "_auth_user_id" not in client.session
    assert client.get(ME).status_code == 401


def test_logout_after_the_session_limit_ends_the_session(
    client: Client, settings: Any
) -> None:
    staff, _ = staff_with_totp()
    client.force_login(staff)
    age_mfa_steps(client.session, settings.ADMIN_SESSION_SECONDS + 5)
    assert client.get(reverse("admin:index")).status_code == 302

    _logged_out(client)


def test_logout_of_a_password_only_staff_session(client: Client) -> None:
    staff, _ = staff_with_totp()
    assert client.login(username=staff.email, password=PASSWORD)

    _logged_out(client)


def test_logout_of_staff_without_mfa(client: Client) -> None:
    staff = verified_account(is_staff=True)
    client.force_login(staff)

    _logged_out(client)

"""Setting another Account's password keeps the staff session's row (#100
delta review).

Django's set-password view cycles the acting staff member's session key
even when the password is another Account's. A staff session signed in
through allauth has a user session row, which must follow the new key, or
the live session drops out of the staff member's list and can no longer
be ended. Made-up data only.
"""

from typing import Any

import pytest
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.urls import reverse

from apps.accounts.tests.admin import (
    sign_in_to_admin,
    staff_with_totp,
    verified_account,
)

pytestmark = pytest.mark.django_db

NEW = "made-up-Passw0rd-w5e1"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


def test_the_staff_row_follows_the_cycled_key() -> None:
    staff, secret = staff_with_totp(is_superuser=True)
    admin = sign_in_to_admin(staff, secret)
    old_key = admin.client.session.session_key
    assert UserSession.objects.filter(user=staff, session_key=old_key).exists()
    target = verified_account()

    response = admin.client.post(
        reverse("admin:auth_user_password_change", args=[target.pk]),
        {
            "usable_password": "true",
            "password1": NEW,
            "password2": NEW,
            "csrfmiddlewaretoken": admin.client.cookies[
                settings.CSRF_COOKIE_NAME
            ].value,
        },
    )

    assert response.status_code == 302
    current = admin.client.session.session_key
    assert list(
        UserSession.objects.filter(user=staff).values_list("session_key", flat=True)
    ) == [current]
    assert not UserSession.objects.filter(user=target).exists()

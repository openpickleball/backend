"""Admin password changes end sessions too (#100 review).

A staff member changing their own password in the admin keeps the admin
session, whose user session row follows its new key, and ends their other
sessions. Staff setting another Account's password end every session of
that Account and delete their user session rows. Made-up data only.
"""

from typing import Any

import pytest
from allauth.usersessions.models import UserSession
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.admin import staff_with_totp, verified_account
from apps.accounts.tests.factories import UserFactory, totp_code
from apps.accounts.tests.headless import APP, PASSWORD, HeadlessSession

pytestmark = pytest.mark.django_db

NEW = "made-up-Passw0rd-w5e1"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


def _app_session(email: str, secret: str | None = None) -> str:
    session = HeadlessSession(APP)
    if secret is None:
        assert session.login(email, PASSWORD).status_code == 200
    else:
        assert session.mfa_login(email, totp_code(secret), PASSWORD).status_code == 200
    assert session.token
    assert UserSession.objects.filter(session_key=session.token).exists()
    return session.token


def test_your_own_change_ends_your_other_sessions(client: Client) -> None:
    staff, secret = staff_with_totp()
    other = _app_session(staff.email, secret)
    client.force_login(staff)
    old_key = client.session.session_key
    UserSession.objects.create(
        user=staff, session_key=old_key, ip="203.0.113.5", user_agent="Made-up/1"
    )

    response = client.post(
        reverse("admin:password_change"),
        {"old_password": PASSWORD, "new_password1": NEW, "new_password2": NEW},
    )

    assert response.status_code == 302
    assert not Session.objects.filter(session_key=other).exists()
    assert not UserSession.objects.filter(session_key=other).exists()
    current = client.session.session_key
    assert current != old_key
    assert list(
        UserSession.objects.filter(user=staff).values_list("session_key", flat=True)
    ) == [current]


def test_setting_another_accounts_password_ends_all_its_sessions(
    client: Client,
) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    target = verified_account()
    tokens = [_app_session(target.email), _app_session(target.email)]

    response = client.post(
        reverse("admin:auth_user_password_change", args=[target.pk]),
        {"usable_password": "true", "password1": NEW, "password2": NEW},
    )

    assert response.status_code == 302
    for token in tokens:
        assert not Session.objects.filter(session_key=token).exists()
    assert not UserSession.objects.filter(user=target).exists()

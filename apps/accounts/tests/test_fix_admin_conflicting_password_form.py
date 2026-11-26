"""A set-password form that changes nothing ends no session (#100 review).

Django's set-password view answers 302 for "Conflicting form data" (a
request to disable the password without the unset-password button) and
saves nothing. That must not end the Account's sessions. Made-up data
only.
"""

from typing import Any

import pytest
from allauth.usersessions.models import UserSession
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.admin import verified_account
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, PASSWORD, HeadlessSession

pytestmark = pytest.mark.django_db


def test_a_conflicting_form_keeps_the_sessions(client: Client) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    target = verified_account()
    session = HeadlessSession(APP)
    assert session.login(target.email, PASSWORD).status_code == 200
    token = session.token
    stored = target.password

    response: Any = client.post(
        reverse("admin:auth_user_password_change", args=[target.pk]),
        {"usable_password": "false", "password1": "", "password2": ""},
    )

    assert response.status_code == 302
    target.refresh_from_db()
    assert target.password == stored
    assert Session.objects.filter(session_key=token).exists()
    assert UserSession.objects.filter(session_key=token).exists()
    assert session.get("auth/session").status_code == 200

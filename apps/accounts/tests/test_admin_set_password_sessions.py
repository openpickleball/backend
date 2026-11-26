"""Which admin set-password submits end the target's sessions (#100).

Disabling password login with the unset-password button changes the
stored hash, so it ends every session of the Account. A form Django
refuses (200) changes nothing and ends nothing. Made-up data only.
"""

import pytest
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.admin import verified_account
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, PASSWORD, HeadlessSession

pytestmark = pytest.mark.django_db


def _target_token() -> tuple[object, str]:
    target = verified_account()
    session = HeadlessSession(APP)
    assert session.login(target.email, PASSWORD).status_code == 200
    assert session.token
    return target, session.token


def _post(client: Client, target: object, data: dict[str, str]) -> int:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    url = reverse("admin:auth_user_password_change", args=[target.pk])  # type: ignore[attr-defined]
    return int(client.post(url, data).status_code)


def test_disabling_password_login_ends_the_sessions(client: Client) -> None:
    target, token = _target_token()

    status = _post(client, target, {"usable_password": "false", "unset-password": "1"})

    assert status == 302
    assert not Session.objects.filter(session_key=token).exists()


def test_a_refused_form_ends_nothing(client: Client) -> None:
    target, token = _target_token()

    status = _post(
        client,
        target,
        {
            "usable_password": "true",
            "password1": "made-up-A1",
            "password2": "different-B2",
        },
    )

    assert status == 200
    assert Session.objects.filter(session_key=token).exists()

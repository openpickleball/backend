"""A password that is not valid UTF-8 text is refused, never a 500 (#100
delta review).

JSON can carry a lone surrogate (``"\\ud800..."``), which Python cannot
encode as UTF-8, so hashing it raised. Every path that sets a password
refuses it with one fixed error, and a login, reauthentication, or
current password check with it fails like a wrong password. Made-up
data only.
"""

import json
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    PASSWORD,
    RESET_PREFIX,
    SIGNUP_DETAILS,
    HeadlessSession,
    app_headers,
    key_from_link,
    link_in_last_mail,
)

pytestmark = pytest.mark.django_db

EMAIL = "surrogate.player@example.org"
BAD = '"\\ud800abcdefgh-XYZ-123"'
CODE = "password_invalid_characters"


@pytest.fixture
def account() -> Any:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    return account


def _raw(client: Client, path: str, body: str, token: str | None = None) -> Any:
    return client.post(
        f"{APP}/{path}",
        body,
        content_type="application/json",
        headers=app_headers(token),
    )


def _codes(response: Any) -> list[str]:
    return [e.get("code") for e in response.json().get("errors", [])]


def _refused(response: Any) -> None:
    assert response.status_code == 400
    assert CODE in _codes(response)
    assert "ud800" not in response.content.decode()


def test_the_password_check_refuses_it() -> None:
    _refused(_raw(Client(), "auth/signup/password-check", f'{{"password": {BAD}}}'))


def test_signup_refuses_it() -> None:
    data = {**SIGNUP_DETAILS, "email": "new.surrogate@example.org", "password": "X"}
    body = json.dumps(data).replace('"password": "X"', f'"password": {BAD}')

    _refused(_raw(Client(), "auth/signup", body))


def test_a_change_refuses_it(account: Any) -> None:
    session = HeadlessSession(APP)
    assert session.login(EMAIL).status_code == 200

    response = _raw(
        session.client,
        "account/password/change",
        f'{{"current_password": "{PASSWORD}", "new_password": {BAD}}}',
        session.token,
    )

    _refused(response)


def test_a_reset_refuses_it(account: Any) -> None:
    session = HeadlessSession(APP)
    assert session.post("auth/password/request", {"email": EMAIL}).status_code == 200
    key = key_from_link(link_in_last_mail(RESET_PREFIX), RESET_PREFIX)

    response = _raw(
        session.client, "auth/password/reset", f'{{"key": "{key}", "password": {BAD}}}'
    )

    _refused(response)


def test_a_login_with_it_fails_like_a_wrong_password(account: Any) -> None:
    response = _raw(
        Client(), "auth/login", f'{{"email": "{EMAIL}", "password": {BAD}}}'
    )

    assert response.status_code == 400


def test_a_current_password_with_it_is_wrong(account: Any) -> None:
    session = HeadlessSession(APP)
    assert session.login(EMAIL).status_code == 200

    response = _raw(
        session.client,
        "account/password/change",
        f'{{"current_password": {BAD}, "new_password": "another-made-up-Passw0rd-2"}}',
        session.token,
    )

    assert response.status_code == 400

"""A passkey login in a session signed in to another Account ends signed
in to the passkey's Account with its passwordless MFA step recorded, so
the session counts as having passed MFA (issue #117). Made-up data
only."""

from typing import Any

import pytest

from apps.accounts.mfa import session_passed_mfa
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    LOGIN_PATH,
    added,
    new_passkey,
    request_options,
    use_passkey_settings,
    verified_account,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_a_passkey_login_over_another_accounts_session(base: str) -> None:
    first = verified_account()
    second = verified_account()
    passkey = new_passkey()
    added(BROWSER, second, passkey)
    session = HeadlessSession(base)
    assert session.login(first.email).status_code == 200

    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["user"]["email"] == second.email
    steps = [
        {key: value for key, value in method.items() if key != "at"}
        for method in data["methods"]
        if method["method"] == "mfa"
    ]
    assert steps == [{"method": "mfa", "type": "webauthn", "passwordless": True}]
    assert session_passed_mfa(session.store(), second)
    current = session.get("auth/session")
    assert current.status_code == 200
    assert current.json()["data"]["user"]["email"] == second.email

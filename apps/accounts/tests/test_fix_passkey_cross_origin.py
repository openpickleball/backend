"""A passkey credential made in a cross-origin frame (``crossOrigin``
true, or any ``topOrigin``) is refused with allauth's 400
``incorrect_code`` on every credential input, even from an allowed
origin: OPEN is never framed (issue #117). Made-up data only."""

from typing import Any

import pytest

from apps.accounts.tests.factories import enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    AUTHENTICATE_PATH,
    LOGIN_PATH,
    REAUTHENTICATE_PATH,
    add,
    added,
    changes,
    error_codes,
    new_passkey,
    passkeys,
    request_options,
    signed_in,
    use_passkey_settings,
    verified_account,
)

pytestmark = pytest.mark.django_db

FRAMED = {
    "cross-origin": {"crossOrigin": True},
    "top-origin": {"topOrigin": "https://evil.example"},
    "both": {"crossOrigin": True, "topOrigin": "https://evil.example"},
    "same-top-origin": {"crossOrigin": False, "topOrigin": "https://app.example.org"},
}


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("framed", list(FRAMED))
def test_a_framed_passkey_login_is_refused(base: str, framed: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    session = HeadlessSession(base)

    credential = passkey.sign_in(
        request_options(session, LOGIN_PATH), client_data=FRAMED[framed]
    )
    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


@pytest.mark.parametrize("framed", list(FRAMED))
def test_a_framed_passkey_is_not_added(framed: str) -> None:
    account = verified_account()
    session = signed_in(BROWSER, account)

    response = add(session, new_passkey(), client_data=FRAMED[framed])

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []
    assert changes(account) == []


@pytest.mark.parametrize("framed", list(FRAMED))
def test_a_framed_reauthentication_is_refused(framed: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    session = added(BROWSER, account, passkey)

    credential = passkey.sign_in(
        request_options(session, REAUTHENTICATE_PATH), client_data=FRAMED[framed]
    )
    response = session.post(REAUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize("framed", list(FRAMED))
def test_a_framed_mfa_stage_is_refused(framed: str) -> None:
    account = verified_account()
    enrollment = enroll_totp(account)
    passkey = new_passkey()
    adder = HeadlessSession(BROWSER)
    adder.mfa_login(account.email, totp_code(enrollment.secret))
    adder.reauthenticate()
    assert add(adder, passkey).status_code == 200
    session = HeadlessSession(BROWSER)
    session.login(account.email)

    credential = passkey.sign_in(
        request_options(session, AUTHENTICATE_PATH), client_data=FRAMED[framed]
    )
    response = session.post(AUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401

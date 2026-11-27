"""A passkey ``credential`` sent as a JSON string, not an object, gets
allauth's 400 ``incorrect_code`` on every credential input, on both
clients (issue #117). allauth's JSONField parses a string with
``json.loads`` and catches only JSONDecodeError, so deeply nested JSON
(RecursionError) or a long run of digits (ValueError) in the string was
a 500. Made-up data only."""

from typing import Any

import pytest

from apps.accounts.tests.factories import enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    AUTHENTICATE_PATH,
    LOGIN_PATH,
    PASSKEY_PATH,
    REAUTHENTICATE_PATH,
    add,
    added,
    creation_options,
    error_codes,
    new_passkey,
    passkeys,
    request_options,
    signed_in,
    use_passkey_settings,
    verified_account,
)

pytestmark = pytest.mark.django_db

STRINGS = {
    "deep-json": "[" * 50000 + "]" * 50000,
    "long-number": "1" * 5000,
    "json-object-text": '{"id": "x"}',
}


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("text", list(STRINGS))
def test_passkey_login_refuses_a_string_credential(base: str, text: str) -> None:
    session = HeadlessSession(base)
    request_options(session, LOGIN_PATH)

    response = session.post(LOGIN_PATH, {"credential": STRINGS[text]})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("text", list(STRINGS))
def test_adding_refuses_a_string_credential(base: str, text: str) -> None:
    account = verified_account()
    session = signed_in(base, account)
    creation_options(session)

    response = session.post(
        PASSKEY_PATH,
        {"name": "Phone", "credential": STRINGS[text], "passwordless": True},
    )

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("text", list(STRINGS))
def test_reauthentication_refuses_a_string_credential(base: str, text: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    session = HeadlessSession(base)
    login = passkey.sign_in(request_options(session, LOGIN_PATH))
    assert session.post(LOGIN_PATH, {"credential": login}).status_code == 200
    request_options(session, REAUTHENTICATE_PATH)

    response = session.post(REAUTHENTICATE_PATH, {"credential": STRINGS[text]})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("text", list(STRINGS))
def test_the_mfa_stage_refuses_a_string_credential(base: str, text: str) -> None:
    account = verified_account()
    enrollment = enroll_totp(account)
    adder = HeadlessSession(BROWSER)
    adder.mfa_login(account.email, totp_code(enrollment.secret))
    adder.reauthenticate()
    assert add(adder, new_passkey()).status_code == 200
    session = HeadlessSession(base)
    session.login(account.email)
    request_options(session, AUTHENTICATE_PATH)

    response = session.post(AUTHENTICATE_PATH, {"credential": STRINGS[text]})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401

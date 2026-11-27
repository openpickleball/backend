"""Every binary member of a passkey credential must be text within its
size cap, or allauth's 400 ``incorrect_code`` answers (issue
#117). fido2 turns a JSON integer N into N zero bytes and a list of
integers into bytes, which skipped the text caps: a large integer made
a pre-authentication allocation, or a MemoryError. Made-up data only."""

import tracemalloc
from collections.abc import Callable
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
    creation_options,
    error_codes,
    new_passkey,
    passkeys,
    post_registration,
    request_options,
    signed_in,
    use_passkey_settings,
    verified_account,
)

pytestmark = pytest.mark.django_db

HUGE = 10**15
Change = Callable[[dict[str, Any]], None]


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def _response_member(name: str, value: object) -> Change:
    def change(credential: dict[str, Any]) -> None:
        credential["response"][name] = value

    return change


def _ids(value: object) -> Change:
    def change(credential: dict[str, Any]) -> None:
        credential["id"] = credential["rawId"] = value

    return change


ASSERTION_CHANGES = {
    "signature-huge": _response_member("signature", HUGE),
    "user-handle-huge": _response_member("userHandle", HUGE),
    "ids-huge": _ids(HUGE),
    "authenticator-data-list": _response_member("authenticatorData", [0] * 20000),
    "signature-list": _response_member("signature", [0] * 20000),
    "client-data-list": _response_member("clientDataJSON", [0] * 100),
}
REGISTRATION_CHANGES = {
    "attestation-huge": _response_member("attestationObject", HUGE),
    "attestation-list": _response_member("attestationObject", [0] * 40000),
    "ids-huge": _ids(HUGE),
}


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("change", list(ASSERTION_CHANGES))
def test_passkey_login_refuses_a_non_text_member(base: str, change: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    session = HeadlessSession(base)
    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    ASSERTION_CHANGES[change](credential)

    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


def test_passkey_login_allocates_nothing_for_an_integer_signature() -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    session = HeadlessSession(BROWSER)
    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    credential["response"]["signature"] = 10**7

    tracemalloc.start()
    try:
        response = session.post(LOGIN_PATH, {"credential": credential})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    # Ten million zero bytes were never made.
    assert peak < 5_000_000


@pytest.mark.parametrize("change", list(ASSERTION_CHANGES))
def test_reauthentication_refuses_a_non_text_member(change: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    session = added(BROWSER, account, passkey)
    credential = passkey.sign_in(request_options(session, REAUTHENTICATE_PATH))
    ASSERTION_CHANGES[change](credential)

    response = session.post(REAUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize("change", list(ASSERTION_CHANGES))
def test_the_mfa_stage_refuses_a_non_text_member(change: str) -> None:
    account = verified_account()
    enrollment = enroll_totp(account)
    passkey = new_passkey()
    adder = HeadlessSession(BROWSER)
    adder.mfa_login(account.email, totp_code(enrollment.secret))
    adder.reauthenticate()
    assert add(adder, passkey).status_code == 200
    session = HeadlessSession(BROWSER)
    session.login(account.email)
    credential = passkey.sign_in(request_options(session, AUTHENTICATE_PATH))
    ASSERTION_CHANGES[change](credential)

    response = session.post(AUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


@pytest.mark.parametrize("change", list(REGISTRATION_CHANGES))
def test_adding_refuses_a_non_text_member(change: str) -> None:
    account = verified_account()
    session = signed_in(BROWSER, account)
    credential = new_passkey().register(creation_options(session))
    REGISTRATION_CHANGES[change](credential)

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []

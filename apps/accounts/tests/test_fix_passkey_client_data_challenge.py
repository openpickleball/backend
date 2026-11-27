"""Client data whose ``challenge`` or ``type`` is not short text gets
allauth's 400 ``incorrect_code`` on every credential input (issue #117).
fido2 decodes the challenge with ``bytes()`` when it is not text, so an
integer challenge in 81 bytes of client data made hundreds of megabytes
before any check, or raised MemoryError. Made-up data only."""

import json
import tracemalloc
from typing import Any

import pytest
from fido2.utils import websafe_encode

from apps.accounts.tests.factories import enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    AUTHENTICATE_PATH,
    LOGIN_PATH,
    PWA,
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

CLIENT_DATA: dict[str, dict[str, object]] = {
    "challenge-huge": {"challenge": 10**15},
    "challenge-list": {"challenge": [0] * 1000},
    "challenge-long": {"challenge": "A" * 4000},
    "type-number": {"type": 1},
}


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def _client_data(kind: str, name: str) -> str:
    data: dict[str, object] = {"type": kind, "challenge": "AAAA", "origin": PWA}
    data.update(CLIENT_DATA[name])
    return websafe_encode(json.dumps(data).encode())


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("name", list(CLIENT_DATA))
def test_passkey_login_refuses_a_bad_challenge(base: str, name: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    session = HeadlessSession(base)
    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    credential["response"]["clientDataJSON"] = _client_data("webauthn.get", name)

    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


def test_an_integer_challenge_allocates_nothing() -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    session = HeadlessSession(BROWSER)
    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    credential["response"]["clientDataJSON"] = websafe_encode(
        json.dumps({"type": "webauthn.get", "challenge": 10**8, "origin": PWA}).encode()
    )

    tracemalloc.start()
    try:
        response = session.post(LOGIN_PATH, {"credential": credential})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert peak < 5_000_000


@pytest.mark.parametrize("name", list(CLIENT_DATA))
def test_reauthentication_refuses_a_bad_challenge(name: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    session = added(BROWSER, account, passkey)
    credential = passkey.sign_in(request_options(session, REAUTHENTICATE_PATH))
    credential["response"]["clientDataJSON"] = _client_data("webauthn.get", name)

    response = session.post(REAUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize("name", list(CLIENT_DATA))
def test_the_mfa_stage_refuses_a_bad_challenge(name: str) -> None:
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
    credential["response"]["clientDataJSON"] = _client_data("webauthn.get", name)

    response = session.post(AUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


@pytest.mark.parametrize("name", list(CLIENT_DATA))
def test_adding_refuses_a_bad_challenge(name: str) -> None:
    account = verified_account()
    session = signed_in(BROWSER, account)
    credential = new_passkey().register(creation_options(session))
    credential["response"]["clientDataJSON"] = _client_data("webauthn.create", name)

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []

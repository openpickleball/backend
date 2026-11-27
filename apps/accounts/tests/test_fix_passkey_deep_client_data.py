"""Deeply nested or oversized client data in a passkey credential gets
allauth's 400 ``incorrect_code`` on every credential input, never a
server error (issue #117). Made-up data only."""

import json
from typing import Any

import pytest
from fido2.utils import websafe_encode

from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    LOGIN_PATH,
    PWA,
    REAUTHENTICATE_PATH,
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


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def _nested_object(depth: int) -> bytes:
    head = json.dumps({"type": "webauthn.get", "origin": PWA})[:-1]
    return f'{head}, "x": {"[" * depth}{"]" * depth}}}'.encode()


CLIENT_DATA = {
    "open-lists": b"[" * 200000,
    "deep-object": _nested_object(100000),
    "small-but-deep": b"[" * 3000,
}


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("name", list(CLIENT_DATA))
def test_passkey_login_refuses_deep_client_data(base: str, name: str) -> None:
    session = HeadlessSession(base)
    credential = new_passkey().sign_in(request_options(session, LOGIN_PATH))
    credential["response"]["clientDataJSON"] = websafe_encode(CLIENT_DATA[name])

    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize("name", list(CLIENT_DATA))
def test_adding_refuses_deep_client_data(name: str) -> None:
    account = verified_account()
    session = signed_in(BROWSER, account)
    credential = new_passkey().register(creation_options(session))
    credential["response"]["clientDataJSON"] = websafe_encode(CLIENT_DATA[name])

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []


@pytest.mark.parametrize("name", list(CLIENT_DATA))
def test_reauthentication_refuses_deep_client_data(name: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    session = added(BROWSER, account, passkey)
    credential = passkey.sign_in(request_options(session, REAUTHENTICATE_PATH))
    credential["response"]["clientDataJSON"] = websafe_encode(CLIENT_DATA[name])

    response = session.post(REAUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]

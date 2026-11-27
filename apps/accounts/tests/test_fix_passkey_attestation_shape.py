"""An attestation object that is not a CBOR map with a text ``fmt``, a
byte string ``authData``, and a map ``attStmt`` is refused with
allauth's 400 ``incorrect_code`` and stores nothing (issue #117). fido2
builds the authenticator data with ``bytes()``, so an integer
``authData`` made memory in proportion to its value, or raised
MemoryError. Made-up data only."""

import tracemalloc
from typing import Any

import pytest
from fido2 import cbor
from fido2.utils import websafe_decode, websafe_encode

from apps.accounts.tests.headless import APP, BROWSER
from apps.accounts.tests.passkey_flows import (
    changes,
    creation_options,
    error_codes,
    new_passkey,
    passkeys,
    post_registration,
    signed_in,
    use_passkey_settings,
    verified_account,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def _shapes(auth_data: bytes) -> dict[str, Any]:
    return {
        "auth-data-huge": {"fmt": "none", "authData": 10**15, "attStmt": {}},
        "auth-data-text": {"fmt": "none", "authData": "x", "attStmt": {}},
        "auth-data-list": {"fmt": "none", "authData": [0] * 64, "attStmt": {}},
        "fmt-number": {"fmt": 1, "authData": auth_data, "attStmt": {}},
        "att-stmt-list": {"fmt": "none", "authData": auth_data, "attStmt": []},
        "no-auth-data": {"fmt": "none", "attStmt": {}},
        "not-a-map": [auth_data],
    }


SHAPES = list(_shapes(b""))


def _registration(base: str, shape: str) -> tuple[Any, Any]:
    account = verified_account()
    session = signed_in(base, account)
    credential = new_passkey().register(creation_options(session))
    real: Any = cbor.decode(websafe_decode(credential["response"]["attestationObject"]))
    value = _shapes(real["authData"])[shape]
    credential["response"]["attestationObject"] = websafe_encode(cbor.encode(value))
    return account, (session, credential)


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("shape", SHAPES)
def test_a_malformed_attestation_object_is_refused(base: str, shape: str) -> None:
    account, (session, credential) = _registration(base, shape)

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []
    assert changes(account) == []


def test_an_integer_auth_data_allocates_nothing() -> None:
    account = verified_account()
    session = signed_in(BROWSER, account)
    credential = new_passkey().register(creation_options(session))
    credential["response"]["attestationObject"] = websafe_encode(
        cbor.encode({"fmt": "none", "authData": 10**8, "attStmt": {}})
    )

    tracemalloc.start()
    try:
        response = post_registration(session, credential)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert peak < 5_000_000
    assert passkeys(account) == []

"""Deeply nested CBOR in a passkey credential (authenticator data
extensions, or an attestation statement) gets allauth's 400
``incorrect_code`` on every credential input, never a server error
(issue #117). Made-up data only."""

from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fido2 import cbor
from fido2.utils import sha256, websafe_decode, websafe_encode
from fido2.webauthn import AuthenticatorData

from apps.accounts.tests.factories import enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    AUTHENTICATE_PATH,
    LOGIN_PATH,
    REAUTHENTICATE_PATH,
    RP_ID,
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
from apps.accounts.tests.webauthn import SoftwarePasskey

pytestmark = pytest.mark.django_db

_FLAG = AuthenticatorData.FLAG
# Nested one-element arrays: past the 4 KiB cap, and under it but deeper
# than Python's recursion limit.
DEPTHS = {"over-the-cap": 50000, "under-the-cap": 3000}


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def _deep(depth: int) -> bytes:
    return b"\x81" * depth + b"\x00"


def _with_deep_extensions(
    passkey: SoftwarePasskey, assertion: dict[str, Any], depth: int
) -> dict[str, Any]:
    """The assertion with authenticator data whose extensions (ED flag)
    are ``depth`` nested arrays, signed again so the signature is valid."""
    flags = _FLAG.UP | _FLAG.UV | _FLAG.ED
    auth_data = (
        sha256(RP_ID.encode())
        + bytes([int(flags)])
        + (passkey.counter + 1).to_bytes(4, "big")
        + _deep(depth)
    )
    client_data = websafe_decode(assertion["response"]["clientDataJSON"])
    signature = passkey.key.sign(
        auth_data + sha256(client_data), ec.ECDSA(hashes.SHA256())
    )
    assertion["response"]["authenticatorData"] = websafe_encode(auth_data)
    assertion["response"]["signature"] = websafe_encode(signature)
    return assertion


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("depth", list(DEPTHS))
def test_passkey_login_refuses_deep_extensions(base: str, depth: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    session = HeadlessSession(base)
    assertion = passkey.sign_in(request_options(session, LOGIN_PATH))

    response = session.post(
        LOGIN_PATH,
        {"credential": _with_deep_extensions(passkey, assertion, DEPTHS[depth])},
    )

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize("depth", list(DEPTHS))
def test_reauthentication_refuses_deep_extensions(depth: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    session = added(BROWSER, account, passkey)
    assertion = passkey.sign_in(request_options(session, REAUTHENTICATE_PATH))

    response = session.post(
        REAUTHENTICATE_PATH,
        {"credential": _with_deep_extensions(passkey, assertion, DEPTHS[depth])},
    )

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize("depth", list(DEPTHS))
def test_the_mfa_stage_refuses_deep_extensions(depth: str) -> None:
    account = verified_account()
    enrollment = enroll_totp(account)
    passkey = new_passkey()
    adder = HeadlessSession(BROWSER)
    adder.mfa_login(account.email, totp_code(enrollment.secret))
    adder.reauthenticate()
    assert add(adder, passkey).status_code == 200
    session = HeadlessSession(BROWSER)
    session.login(account.email)
    assertion = passkey.sign_in(request_options(session, AUTHENTICATE_PATH))

    response = session.post(
        AUTHENTICATE_PATH,
        {"credential": _with_deep_extensions(passkey, assertion, DEPTHS[depth])},
    )

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


@pytest.mark.parametrize("depth", list(DEPTHS))
def test_adding_refuses_a_deep_attestation_statement(depth: str) -> None:
    account = verified_account()
    session = signed_in(BROWSER, account)
    credential = new_passkey().register(creation_options(session))
    attestation: Any = cbor.decode(
        websafe_decode(credential["response"]["attestationObject"])
    )
    # A CBOR map of three entries, with attStmt replaced by deep arrays.
    deep = (
        b"\xa3"
        + cbor.encode("fmt")
        + cbor.encode("none")
        + cbor.encode("attStmt")
        + _deep(DEPTHS[depth])
        + cbor.encode("authData")
        + cbor.encode(attestation["authData"])
    )
    credential["response"]["attestationObject"] = websafe_encode(deep)

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []

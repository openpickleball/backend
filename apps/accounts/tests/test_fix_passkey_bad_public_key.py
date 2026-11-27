"""A passkey whose COSE public key is malformed (an ES256 key without
its x coordinate, or with integer coordinates) is refused when added,
and a stored one gets allauth's 400 ``incorrect_code`` on passkey
login, the MFA login stage, and reauthentication, never a server error
(issue #117). Made-up data only."""

import os
from typing import TYPE_CHECKING, Any

import pytest
from allauth.mfa.models import Authenticator
from fido2.utils import sha256, websafe_decode, websafe_encode
from fido2.webauthn import (
    AttestationObject,
    AttestedCredentialData,
    AuthenticatorData,
    CollectedClientData,
)

from apps.accounts.passkeys import user_handle
from apps.accounts.tests.factories import enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    AUTHENTICATE_PATH,
    LOGIN_PATH,
    PWA,
    REAUTHENTICATE_PATH,
    RP_ID,
    changes,
    creation_options,
    error_codes,
    passkeys,
    post_registration,
    request_options,
    signed_in,
    use_passkey_settings,
    verified_account,
)
from apps.accounts.tests.webauthn import SoftwarePasskey

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

_FLAG = AuthenticatorData.FLAG
BAD_KEYS: dict[str, dict[int, Any]] = {
    "no-x": {1: 2, 3: -7, -1: 1, -3: b"\x01" * 32},
    "int-coordinates": {1: 2, 3: -7, -1: 1, -2: 5, -3: 6},
}


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def _registration(
    challenge: bytes, credential_id: bytes, cose: dict[int, Any]
) -> dict[str, Any]:
    """A registration response carrying ``cose`` as its public key."""
    attested = AttestedCredentialData.create(
        b"\0" * 16,
        credential_id,
        cose,  # type: ignore[arg-type]
    )
    auth_data = AuthenticatorData.create(
        sha256(RP_ID.encode()), _FLAG.UP | _FLAG.UV | _FLAG.AT, 0, attested
    )
    attestation = AttestationObject.create("none", auth_data, {})
    client_data = CollectedClientData.create("webauthn.create", challenge, PWA)
    encoded_id = websafe_encode(credential_id)
    return {
        "id": encoded_id,
        "rawId": encoded_id,
        "type": "public-key",
        "response": {
            "clientDataJSON": websafe_encode(client_data),
            "attestationObject": websafe_encode(attestation),
        },
        "clientExtensionResults": {"credProps": {"rk": True}},
    }


def _stored_bad_passkey(account: "_User", key: str) -> SoftwarePasskey:
    """A passkey row with a malformed public key, stored as an older
    release would have, and a software passkey that uses its id."""
    credential_id = os.urandom(32)
    Authenticator.objects.create(
        user=account,
        type=Authenticator.Type.WEBAUTHN,
        data={
            "name": "Old",
            "credential": _registration(b"made-up", credential_id, BAD_KEYS[key]),
        },
    )
    return SoftwarePasskey(
        rp_id=RP_ID,
        origin=PWA,
        credential_id=credential_id,
        user_handle=user_handle(account),
    )


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("key", list(BAD_KEYS))
def test_a_malformed_public_key_is_not_added(base: str, key: str) -> None:
    account = verified_account()
    session = signed_in(base, account)
    options = creation_options(session)["publicKey"]
    credential = _registration(
        websafe_decode(options["challenge"]), os.urandom(32), BAD_KEYS[key]
    )

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []
    assert changes(account) == []


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("key", list(BAD_KEYS))
def test_passkey_login_with_a_stored_bad_key_is_refused(base: str, key: str) -> None:
    account = verified_account()
    passkey = _stored_bad_passkey(account, key)
    session = HeadlessSession(base)

    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


@pytest.mark.parametrize("key", list(BAD_KEYS))
def test_the_mfa_stage_with_a_stored_bad_key_is_refused(key: str) -> None:
    account = verified_account()
    passkey = _stored_bad_passkey(account, key)
    session = HeadlessSession(BROWSER)
    session.login(account.email)

    credential = passkey.sign_in(request_options(session, AUTHENTICATE_PATH))
    response = session.post(AUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


@pytest.mark.parametrize("key", list(BAD_KEYS))
def test_reauthentication_with_a_stored_bad_key_is_refused(key: str) -> None:
    account = verified_account()
    enrollment = enroll_totp(account)
    passkey = _stored_bad_passkey(account, key)
    session = HeadlessSession(BROWSER)
    session.mfa_login(account.email, totp_code(enrollment.secret))

    credential = passkey.sign_in(request_options(session, REAUTHENTICATE_PATH))
    response = session.post(REAUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]

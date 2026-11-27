"""The software passkey in ``webauthn.py`` makes credentials that fido2,
allauth's WebAuthn library, accepts (issue #117)."""

import pytest
from fido2.server import Fido2Server
from fido2.webauthn import (
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialUserEntity,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from apps.accounts.tests.webauthn import SoftwarePasskey

RP_ID = "example.org"
ORIGIN = "https://app.example.org"


def _server() -> Fido2Server:
    return Fido2Server(PublicKeyCredentialRpEntity(id=RP_ID, name="OPEN"))


def _register(server: Fido2Server, passkey: SoftwarePasskey) -> object:
    options, state = server.register_begin(
        PublicKeyCredentialUserEntity(
            id=b"made-up-handle", name="robin@example.org", display_name="Robin"
        ),
        resident_key_requirement=ResidentKeyRequirement.REQUIRED,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return server.register_complete(state, passkey.register(dict(options)))


def test_a_made_credential_registers_and_signs_in() -> None:
    server = _server()
    passkey = SoftwarePasskey(rp_id=RP_ID, origin=ORIGIN)
    binding = _register(server, passkey)
    credential_data = binding.credential_data  # type: ignore[attr-defined]
    assert credential_data.credential_id == passkey.credential_id
    assert passkey.user_handle == b"made-up-handle"

    options, state = server.authenticate_begin(
        [credential_data], user_verification=UserVerificationRequirement.REQUIRED
    )
    assertion = passkey.sign_in(dict(options))

    assert server.authenticate_complete(state, [credential_data], assertion)
    assert assertion["response"]["userHandle"]


def test_a_claimed_origin_outside_the_rp_id_is_refused() -> None:
    server = _server()
    passkey = SoftwarePasskey(rp_id=RP_ID, origin="https://app.example.net")
    with pytest.raises(ValueError, match="origin"):
        _register(server, passkey)

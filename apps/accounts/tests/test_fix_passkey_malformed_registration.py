"""A malformed passkey registration gets allauth's 400 ``incorrect_code``
and stores nothing, never a server error (issue #117). Made-up data
only."""

from typing import Any

import pytest
from fido2.utils import sha256, websafe_encode
from fido2.webauthn import AttestationObject, AuthenticatorData

from apps.accounts.tests.headless import APP, BROWSER
from apps.accounts.tests.passkey_flows import (
    RP_ID,
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

_FLAG = AuthenticatorData.FLAG


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("attestation", ["", "!!!", "u2Uo"])
def test_a_malformed_attestation_object_is_refused(base: str, attestation: str) -> None:
    account = verified_account()
    session = signed_in(base, account)
    credential = new_passkey().register(creation_options(session))
    credential["response"]["attestationObject"] = attestation

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []
    assert changes(account) == []


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_a_registration_without_credential_data_is_refused(base: str) -> None:
    account = verified_account()
    session = signed_in(base, account)
    credential = new_passkey().register(creation_options(session))
    # User present and verified, but no attested credential data (no AT).
    auth_data = AuthenticatorData.create(sha256(RP_ID.encode()), _FLAG.UP | _FLAG.UV, 0)
    credential["response"]["attestationObject"] = websafe_encode(
        AttestationObject.create("none", auth_data, {})
    )

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []
    assert changes(account) == []

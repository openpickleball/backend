"""A passkey with an ML-DSA-87 key (a 2592-byte public key, 4627-byte
signatures), which fido2 offers through allauth's server when the
installed cryptography supports it, can be added and then used for
passkey login and reauthentication: the size caps leave room for it
(issue #117). Made-up data only."""

from typing import Any

import pytest

from apps.accounts.mfa import session_passed_mfa
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    LOGIN_PATH,
    PWA,
    REAUTHENTICATE_PATH,
    RP_ID,
    add,
    creation_options,
    passkeys,
    request_options,
    signed_in,
    use_passkey_settings,
    verified_account,
)
from apps.accounts.tests.webauthn import MLDSA87Passkey, mldsa87_available

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        not mldsa87_available(), reason="cryptography has no ML-DSA-87 here"
    ),
]


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def test_the_server_offers_ml_dsa_87() -> None:
    account = verified_account()
    session = signed_in(BROWSER, account)

    params = creation_options(session)["publicKey"]["pubKeyCredParams"]

    assert -50 in [param["alg"] for param in params]


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_an_ml_dsa_87_passkey_signs_in_and_reauthenticates(base: str) -> None:
    account = verified_account()
    passkey = MLDSA87Passkey(rp_id=RP_ID, origin=PWA)
    adder = signed_in(BROWSER, account)
    added = add(adder, passkey)
    assert added.status_code == 200, added.content
    assert len(passkeys(account)) == 1
    session = HeadlessSession(base)

    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    assert len(credential["response"]["signature"]) > 6000
    login = session.post(LOGIN_PATH, {"credential": credential})

    assert login.status_code == 200, login.content
    assert session_passed_mfa(session.store(), account)
    reauth = passkey.sign_in(request_options(session, REAUTHENTICATE_PATH))
    response = session.post(REAUTHENTICATE_PATH, {"credential": reauth})
    assert response.status_code == 200, response.content

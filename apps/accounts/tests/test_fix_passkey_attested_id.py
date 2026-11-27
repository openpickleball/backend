"""A passkey registration whose ``id`` and ``rawId`` are not the attested
credential id is refused with allauth's 400 ``incorrect_code`` and
stores nothing, so the stored id the login lookup and the uniqueness
check read is always the real credential id (issue #117). Made-up data
only."""

from typing import Any

import pytest
from fido2.utils import websafe_encode

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


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_an_id_that_is_not_the_attested_credential_id_is_refused(base: str) -> None:
    account = verified_account()
    session = signed_in(base, account)
    credential = new_passkey().register(creation_options(session))
    other = websafe_encode(b"a-made-up-credential-id-not-attested")
    credential["id"] = credential["rawId"] = other

    response = post_registration(session, credential)

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert passkeys(account) == []
    assert changes(account) == []

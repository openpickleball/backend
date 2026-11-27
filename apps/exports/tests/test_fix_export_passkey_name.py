"""The data export lists each passkey with the name the player chose
(issue #117, ADR 0056), and never its credential, public key, or user
handle. Made-up data only."""

import json
from typing import Any

import pytest
from fido2.utils import websafe_encode

from apps.accounts.passkeys import user_handle
from apps.accounts.tests.headless import BROWSER
from apps.accounts.tests.passkey_flows import (
    added,
    new_passkey,
    passkeys,
    use_passkey_settings,
    verified_account,
)
from apps.exports.builder import build_export

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def test_a_passkey_is_exported_with_its_name_only() -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    [row] = passkeys(account)

    data = json.loads(build_export(account).model_dump_json())

    [exported] = [m for m in data["mfa_methods"] if m["type"] == "webauthn"]
    assert exported["name"] == "Phone"
    assert set(exported) == {"type", "name", "created_at", "last_used_at"}
    text = json.dumps(data)
    credential = row.data["credential"]
    for secret_or_id in (
        passkey.id,
        credential["response"]["attestationObject"],
        credential["response"]["clientDataJSON"],
        websafe_encode(user_handle(account)),
        user_handle(account).decode(),
    ):
        assert secret_or_id not in text
    [codes] = [m for m in data["mfa_methods"] if m["type"] == "recovery_codes"]
    assert codes["name"] is None

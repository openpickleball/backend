"""OPEN's passkey helpers and MFA adapter (issue #117, ADR 0056).

The relying party is ``WEBAUTHN_RP_ID``, never the request's host. The
WebAuthn user handle and the passkey ids the API shows are keyed hashes,
never a row id. A credential's origin must be exactly an allowed origin.
"""

import base64
import json
from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.utils import user_pk_to_url_str
from allauth.mfa.models import Authenticator
from django.core.exceptions import ValidationError
from django.test import RequestFactory

from apps.accounts import passkeys
from apps.accounts.mfa_adapter import MFAAdapter
from apps.accounts.tests.factories import UserFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

ORIGIN = "https://app.example.org"


@pytest.fixture(autouse=True)
def _webauthn_settings(settings: Any) -> None:
    settings.WEBAUTHN_RP_ID = "example.org"
    settings.WEBAUTHN_ALLOWED_ORIGINS = [ORIGIN]


def _passkey(account: "_User", name: str = "Phone") -> Authenticator:
    authenticator: Authenticator = Authenticator.objects.create(
        user=account,
        type=Authenticator.Type.WEBAUTHN,
        data={"name": name, "credential": {}},
    )
    return authenticator


def _client_data(value: object) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _credential(client_data: str) -> dict[str, Any]:
    return {"response": {"clientDataJSON": client_data}}


def test_rp_entity_is_the_setting_not_the_host(rf: RequestFactory) -> None:
    from allauth.core import context

    request = rf.get("/", HTTP_HOST="api.example.org")
    with context.request_context(request):
        entity = MFAAdapter().get_public_key_credential_rp_entity()

    assert entity == {"id": "example.org", "name": "OPEN"}


def test_user_entity_id_is_the_keyed_handle_not_the_row_id() -> None:
    account = UserFactory.create()

    entity = MFAAdapter().get_public_key_credential_user_entity(account)

    assert entity["id"] == passkeys.user_handle(account)
    assert entity["id"] != user_pk_to_url_str(account).encode()
    assert len(entity["id"]) <= 64
    assert entity["id"].isascii()
    assert entity["name"] == account.email


def test_user_handles_differ_between_accounts() -> None:
    first, second = UserFactory.create_batch(2)

    assert passkeys.user_handle(first) != passkeys.user_handle(second)
    assert passkeys.user_handle_matches(first, passkeys.user_handle(first))
    assert not passkeys.user_handle_matches(first, passkeys.user_handle(second))
    assert not passkeys.user_handle_matches(first, b"")


def test_a_handle_made_with_a_fallback_key_still_matches(settings: Any) -> None:
    account = UserFactory.create()
    old_handle = passkeys.user_handle(account)

    settings.SECRET_KEY_FALLBACKS = [settings.SECRET_KEY]
    settings.SECRET_KEY = "made-up-rotated-key-" + "r" * 40

    assert passkeys.user_handle(account) != old_handle
    assert passkeys.user_handle_matches(account, old_handle)
    settings.SECRET_KEY_FALLBACKS = []
    assert not passkeys.user_handle_matches(account, old_handle)


def test_public_passkey_ids_are_opaque_and_per_passkey() -> None:
    account = UserFactory.create()
    first, second = _passkey(account), _passkey(account, "Laptop")

    first_id = passkeys.public_passkey_id(first)

    assert len(first_id) == 32
    assert int(first_id, 16) >= 0
    assert first_id != str(first.pk)
    assert first_id != passkeys.public_passkey_id(second)


def test_passkeys_by_public_id_holds_only_the_accounts_own_passkeys() -> None:
    account, other = UserFactory.create_batch(2)
    own = _passkey(account)
    _passkey(other)
    Authenticator.objects.create(
        user=account, type=Authenticator.Type.TOTP, data={"secret": "x"}
    )

    found = passkeys.passkeys_by_public_id(account)

    assert found == {passkeys.public_passkey_id(own): own}


def test_client_data_origin_reads_the_origin() -> None:
    credential = _credential(_client_data({"type": "webauthn.get", "origin": ORIGIN}))

    assert passkeys.client_data_origin(credential) == ORIGIN


@pytest.mark.parametrize(
    "credential",
    [
        None,
        "text",
        [],
        {},
        {"response": "text"},
        {"response": {}},
        {"response": {"clientDataJSON": 12}},
        {"response": {"clientDataJSON": "***"}},
        _credential(_client_data(b"\xff\xfe not json")),
        _credential(_client_data(["origin", ORIGIN])),
        _credential(_client_data({"type": "webauthn.get"})),
        _credential(_client_data({"origin": 42})),
    ],
)
def test_client_data_origin_is_none_for_malformed_input(credential: object) -> None:
    assert passkeys.client_data_origin(credential) is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://www.example.org",
        "https://example.org",
        "http://app.example.org",
        "https://app.example.org:8443",
        "https://app.example.org/",
        "https://APP.example.org",
    ],
)
def test_check_origin_refuses_any_other_origin(origin: str) -> None:
    credential = _credential(_client_data({"origin": origin}))

    with pytest.raises(ValidationError) as raised:
        passkeys.check_origin(credential)

    assert raised.value.code == "incorrect_code"


def test_check_origin_refuses_malformed_client_data() -> None:
    with pytest.raises(ValidationError) as raised:
        passkeys.check_origin({"response": {}})

    assert raised.value.code == "incorrect_code"


def test_check_origin_accepts_an_allowed_origin() -> None:
    passkeys.check_origin(_credential(_client_data({"origin": ORIGIN})))

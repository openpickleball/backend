"""Passkeys through allauth's headless API, with OPEN's rules (issue #117,
ADR 0056). Made-up data and a software passkey only.

A player adds a passkey after a recent reauthentication, and can then
sign in with it alone (passkey login), use it at the MFA login stage, or
reauthenticate with it. The relying party is ``WEBAUTHN_RP_ID``, only
the exact ``WEBAUTHN_ALLOWED_ORIGINS`` pass, no row id reaches a
response, an option, or the session, and every change is audited.
"""

from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.authentication import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str
from allauth.mfa.models import Authenticator
from django.contrib.sessions.backends.base import SessionBase
from fido2.utils import websafe_decode, websafe_encode

from apps.accounts.mfa import has_mfa, mfa_status, session_passed_mfa
from apps.accounts.passkeys import public_passkey_id, user_handle
from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    HeadlessSession,
)
from apps.accounts.tests.webauthn import SoftwarePasskey
from apps.audit.models import AuditAction, AuditEvent

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

RP_ID = "example.org"
PWA = "https://app.example.org"
# A sibling subdomain of the relying party ID: fido2 alone would pass it.
SIBLING = "https://evil.example.org"
PASSKEY_PATH = "account/authenticators/webauthn"
LOGIN_PATH = "auth/webauthn/login"
AUTHENTICATE_PATH = "auth/webauthn/authenticate"
REAUTHENTICATE_PATH = "auth/webauthn/reauthenticate"
UNKNOWN_PASSKEY = "Select passkeys from your passkey list."


@pytest.fixture(autouse=True)
def _webauthn_settings(settings: Any) -> None:
    settings.WEBAUTHN_RP_ID = RP_ID
    settings.WEBAUTHN_ALLOWED_ORIGINS = [PWA]
    settings.MFA_TOTP_TOLERANCE = 1


@pytest.fixture(params=[BROWSER, APP], ids=["browser", "app"])
def base(request: pytest.FixtureRequest) -> str:
    param: str = request.param
    return param


@pytest.fixture
def account() -> "_User":
    account = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


@pytest.fixture
def passkey() -> SoftwarePasskey:
    return SoftwarePasskey(rp_id=RP_ID, origin=PWA)


def _signed_in(base: str, account: "_User") -> HeadlessSession:
    session = HeadlessSession(base)
    assert session.login(account.email).status_code == 200
    assert session.reauthenticate().status_code == 200
    return session


def _creation_options(
    session: HeadlessSession, passwordless: bool = True
) -> dict[str, Any]:
    path = f"{PASSKEY_PATH}?passwordless" if passwordless else PASSKEY_PATH
    response = session.get(path)
    assert response.status_code == 200, response.content
    options: dict[str, Any] = response.json()["data"]["creation_options"]
    return options


def _add(
    session: HeadlessSession,
    passkey: SoftwarePasskey,
    *,
    name: str = "Phone",
    origin: str | None = None,
    passwordless: bool = True,
) -> Any:
    credential = passkey.register(
        _creation_options(session, passwordless), origin=origin
    )
    return session.post(
        PASSKEY_PATH,
        {"name": name, "credential": credential, "passwordless": passwordless},
    )


def _added(base: str, account: "_User", passkey: SoftwarePasskey) -> HeadlessSession:
    session = _signed_in(base, account)
    response = _add(session, passkey)
    assert response.status_code == 200, response.content
    return session


def _request_options(session: HeadlessSession, path: str) -> dict[str, Any]:
    response = session.get(path)
    assert response.status_code == 200, response.content
    options: dict[str, Any] = response.json()["data"]["request_options"]
    return options


def _passkey_login(
    base: str, passkey: SoftwarePasskey, **assertion: Any
) -> tuple[HeadlessSession, Any]:
    session = HeadlessSession(base)
    credential = passkey.sign_in(_request_options(session, LOGIN_PATH), **assertion)
    return session, session.post(LOGIN_PATH, {"credential": credential})


def _changes(account: "_User") -> list[Any]:
    return [
        event.after
        for event in AuditEvent.objects.filter(
            action=AuditAction.MFA_CHANGE, actor_user_id=account.pk
        ).order_by("pk")
    ]


def _passkeys(account: "_User") -> list[Authenticator]:
    return list(
        Authenticator.objects.filter(
            user_id=account.pk, type=Authenticator.Type.WEBAUTHN
        ).order_by("pk")
    )


def _store(session: HeadlessSession) -> SessionBase:
    return session.store()


def _error_codes(response: Any) -> list[str]:
    return [error["code"] for error in response.json()["errors"]]


def _find_ids(value: object) -> list[object]:
    """Every value under an ``id`` key, at any depth."""
    if isinstance(value, dict):
        found = [value["id"]] if "id" in value else []
        for item in value.values():
            found += _find_ids(item)
        return found
    if isinstance(value, list):
        return [found for item in value for found in _find_ids(item)]
    return []


# Adding, and signing in with a passkey alone (acceptance criterion 1).


def test_a_player_adds_a_passkey_and_signs_in_with_it(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    session = _added(BROWSER, account, passkey)
    assert session.logout().status_code == 401

    signed_in, response = _passkey_login(BROWSER, passkey)

    assert response.status_code == 200, response.content
    assert response.json()["meta"]["is_authenticated"] is True
    assert response.json()["data"]["user"]["email"] == account.email
    store = _store(signed_in)
    assert session_passed_mfa(store, account)
    methods = response.json()["data"]["methods"]
    assert [m for m in methods if m["method"] == "mfa"] == [
        {
            "method": "mfa",
            "at": methods[-1]["at"],
            "type": "webauthn",
            "passwordless": True,
        }
    ]
    current = signed_in.get("auth/session")
    assert current.status_code == 200
    assert current.json()["data"]["user"]["email"] == account.email


def test_the_added_passkey_answer_has_a_public_id(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    session = _signed_in(base, account)

    response = _add(session, passkey)

    assert response.status_code == 200, response.content
    [row] = _passkeys(account)
    data = response.json()["data"]
    assert data["id"] == public_passkey_id(row)
    assert data["name"] == "Phone"
    assert data["type"] == "webauthn"
    assert data["is_passwordless"] is True
    assert response.json()["meta"] == {"recovery_codes_generated": True}


def test_creation_options_name_the_rp_id_and_the_keyed_handle(
    base: str, account: "_User"
) -> None:
    session = _signed_in(base, account)

    options = _creation_options(session)["publicKey"]

    assert options["rp"] == {"id": RP_ID, "name": "OPEN"}
    assert websafe_decode(options["user"]["id"]) == user_handle(account)
    assert options["user"]["id"] != websafe_encode(user_pk_to_url_str(account).encode())


def test_request_options_name_the_rp_id(base: str) -> None:
    options = _request_options(HeadlessSession(base), LOGIN_PATH)["publicKey"]

    assert options["rpId"] == RP_ID


def test_a_passkey_alone_counts_as_mfa(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)

    assert has_mfa(account)
    assert {row.type for row in Authenticator.objects.filter(user_id=account.pk)} == {
        Authenticator.Type.WEBAUTHN,
        Authenticator.Type.RECOVERY_CODES,
    }


def test_a_passkey_satisfies_the_required_mfa_rule(passkey: SoftwarePasskey) -> None:
    staff = UserFactory.create(password=PASSWORD, is_staff=True, mfa=False)
    EmailAddress.objects.create(
        user=staff, email=staff.email, verified=True, primary=True
    )
    assert mfa_status(staff).required
    assert not mfa_status(staff).enrolled

    _added(BROWSER, staff, passkey)

    assert mfa_status(staff).enrolled


def test_the_adding_session_keeps_api_access(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    session = _added(BROWSER, account, passkey)

    assert session_passed_mfa(_store(session), account)
    response = session.client.get("/api/v1/accounts/me")
    assert response.status_code == 200


# The relying party and origins (criteria 2 and 3).


def test_a_passkey_from_a_sibling_origin_is_not_added(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    session = _signed_in(base, account)
    options = _creation_options(session)

    refused = session.post(
        PASSKEY_PATH,
        {
            "name": "Phone",
            "credential": passkey.register(options, origin=SIBLING),
            "passwordless": True,
        },
    )

    assert refused.status_code == 400
    assert _error_codes(refused) == ["incorrect_code"]
    assert _passkeys(account) == []
    assert _changes(account) == []
    # The refusal consumed no challenge: the same options still work.
    accepted = session.post(
        PASSKEY_PATH,
        {
            "name": "Phone",
            "credential": passkey.register(options),
            "passwordless": True,
        },
    )
    assert accepted.status_code == 200, accepted.content


def test_a_passkey_login_from_a_sibling_origin_is_refused(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)
    before = _changes(account)

    session, response = _passkey_login(base, passkey, origin=SIBLING)

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401
    assert _changes(account) == before
    assert _passkeys(account)[0].last_used_at is None


def test_the_app_client_login_is_refused_for_a_native_origin(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)

    _, response = _passkey_login(
        APP, passkey, origin="android:apk-key-hash:made-up-hash"
    )

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]


def test_the_mfa_stage_refuses_a_sibling_origin(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    enrollment = enroll_totp(account)
    adder = HeadlessSession(BROWSER)
    adder.mfa_login(account.email, totp_code(enrollment.secret))
    adder.reauthenticate()
    assert _add(adder, passkey).status_code == 200
    session = HeadlessSession(base)
    session.login(account.email)

    credential = passkey.sign_in(
        _request_options(session, AUTHENTICATE_PATH), origin=SIBLING
    )
    response = session.post(AUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


def test_reauthentication_refuses_a_sibling_origin(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)
    session, _ = _passkey_login(base, passkey)

    credential = passkey.sign_in(
        _request_options(session, REAUTHENTICATE_PATH), origin=SIBLING
    )
    response = session.post(REAUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]


# The MFA login stage and reauthentication with a passkey.


def test_a_passkey_passes_the_mfa_login_stage(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    enrollment = enroll_totp(account)
    adder = HeadlessSession(BROWSER)
    adder.mfa_login(account.email, totp_code(enrollment.secret))
    adder.reauthenticate()
    assert _add(adder, passkey).status_code == 200
    session = HeadlessSession(base)
    session.login(account.email)

    credential = passkey.sign_in(_request_options(session, AUTHENTICATE_PATH))
    response = session.post(AUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 200, response.content
    assert response.json()["meta"]["is_authenticated"] is True
    assert session_passed_mfa(_store(session), account)
    assert _find_ids(response.json()["data"]["methods"]) == []


def test_a_passkey_reauthenticates(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)
    session, _ = _passkey_login(base, passkey)

    credential = passkey.sign_in(_request_options(session, REAUTHENTICATE_PATH))
    response = session.post(REAUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 200, response.content
    # A recent reauthentication: the passkey list can now be changed.
    renamed = session.put_json(
        PASSKEY_PATH,
        {"id": public_passkey_id(_passkeys(account)[0]), "name": "Laptop"},
    )
    assert renamed.status_code == 200, renamed.content


# Reauthentication first and the session rule (criterion 4).


@pytest.mark.parametrize(
    ("method", "body"),
    [
        ("get", None),
        ("post", {"credential": {"made": "up"}}),
        ("put_json", {"id": "made-up", "name": "x"}),
        ("delete_json", {"authenticators": ["made-up"]}),
    ],
)
def test_changes_need_a_recent_reauthentication_first(
    base: str, account: "_User", method: str, body: dict[str, Any] | None
) -> None:
    session = HeadlessSession(base)
    session.login(account.email)

    call = getattr(session, method)
    response = call(PASSKEY_PATH) if body is None else call(PASSKEY_PATH, body)

    assert response.status_code == 401
    flows = [flow["id"] for flow in response.json()["data"]["flows"]]
    assert "reauthenticate" in flows
    assert _passkeys(account) == []
    assert _changes(account) == []


@pytest.mark.parametrize("method", ["get", "post", "put_json", "delete_json"])
def test_a_session_that_skipped_mfa_gets_only_the_mfa_flow(
    account: "_User", method: str
) -> None:
    enroll_totp(account)
    session = HeadlessSession(BROWSER)
    # A password-only session (Django's own login).
    assert session.client.login(username=account.email, password=PASSWORD)
    session.reauthenticate()

    call = getattr(session, method)
    response = call(PASSKEY_PATH) if method == "get" else call(PASSKEY_PATH, {})

    assert response.status_code == 401
    assert [flow["id"] for flow in response.json()["data"]["flows"]] == [
        "mfa_reauthenticate"
    ]


def test_an_unverified_address_cannot_add_a_passkey(
    passkey: SoftwarePasskey,
) -> None:
    account = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=False, primary=True
    )
    session = HeadlessSession(BROWSER)
    assert session.client.login(username=account.email, password=PASSWORD)
    session.reauthenticate()

    assert session.get(PASSKEY_PATH).status_code == 409


# Audit and ending sessions (criterion 5).


def test_adding_records_the_passkey_and_the_codes(base: str, account: "_User") -> None:
    session = _signed_in(base, account)

    assert _add(session, SoftwarePasskey(rp_id=RP_ID, origin=PWA)).status_code == 200
    assert _add(session, SoftwarePasskey(rp_id=RP_ID, origin=PWA)).status_code == 200

    assert _changes(account) == [
        {"change": "added", "type": "webauthn"},
        {"change": "added", "type": "recovery_codes"},
        {"change": "added", "type": "webauthn"},
    ]


def test_removing_the_last_method_records_the_codes_and_ends_other_sessions(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    session = _added(base, account, passkey)
    other, _ = _passkey_login(BROWSER, passkey)
    assert other.get("auth/session").status_code == 200
    [row] = _passkeys(account)

    response = session.delete_json(
        PASSKEY_PATH, {"authenticators": [public_passkey_id(row)]}
    )

    assert response.status_code == 200, response.content
    assert _passkeys(account) == []
    assert not Authenticator.objects.filter(user_id=account.pk).exists()
    assert _changes(account)[-2:] == [
        {"change": "removed", "type": "webauthn"},
        {"change": "removed", "type": "recovery_codes"},
    ]
    assert other.get("auth/session").status_code == 401
    assert session.get("auth/session").status_code == 200


def test_removing_one_of_two_methods_keeps_the_codes(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    session = _added(BROWSER, account, passkey)
    second = SoftwarePasskey(rp_id=RP_ID, origin=PWA)
    assert _add(session, second, name="Laptop").status_code == 200
    first = _passkeys(account)[0]

    response = session.delete_json(
        PASSKEY_PATH, {"authenticators": [public_passkey_id(first)]}
    )

    assert response.status_code == 200
    assert _changes(account)[-1] == {"change": "removed", "type": "webauthn"}
    assert Authenticator.objects.filter(
        user_id=account.pk, type=Authenticator.Type.RECOVERY_CODES
    ).exists()


def test_removing_both_passkeys_records_each(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    session = _added(BROWSER, account, passkey)
    assert _add(session, SoftwarePasskey(rp_id=RP_ID, origin=PWA)).status_code == 200
    ids = [public_passkey_id(row) for row in _passkeys(account)]

    response = session.delete_json(PASSKEY_PATH, {"authenticators": ids})

    assert response.status_code == 200
    assert _changes(account)[-3:] == [
        {"change": "removed", "type": "webauthn"},
        {"change": "removed", "type": "webauthn"},
        {"change": "removed", "type": "recovery_codes"},
    ]


def test_renaming_records_nothing(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    session = _added(base, account, passkey)
    before = _changes(account)
    [row] = _passkeys(account)

    response = session.put_json(
        PASSKEY_PATH, {"id": public_passkey_id(row), "name": "Laptop"}
    )

    assert response.status_code == 200, response.content
    assert response.json()["data"]["id"] == public_passkey_id(row)
    assert response.json()["data"]["name"] == "Laptop"
    row.refresh_from_db()
    assert row.wrap().name == "Laptop"
    assert _changes(account) == before


def test_a_passkey_login_and_reauthentication_record_nothing(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)
    before = _changes(account)

    session, _ = _passkey_login(BROWSER, passkey)
    credential = passkey.sign_in(_request_options(session, REAUTHENTICATE_PATH))
    session.post(REAUTHENTICATE_PATH, {"credential": credential})

    assert _changes(account) == before


# No row ids (criterion 6).


@pytest.mark.parametrize(
    "sent",
    [
        lambda row, other: row.pk,
        lambda row, other: str(row.pk),
        lambda row, other: public_passkey_id(other),
        lambda row, other: 42,
        lambda row, other: None,
        lambda row, other: "",
        lambda row, other: [public_passkey_id(row)],
    ],
    ids=["row-id", "row-id-text", "other", "number", "none", "empty", "list"],
)
def test_rename_accepts_only_the_players_public_ids(
    account: "_User", passkey: SoftwarePasskey, sent: Any
) -> None:
    session = _added(BROWSER, account, passkey)
    [row] = _passkeys(account)
    other = UserFactory.create()
    other_row = Authenticator.objects.create(
        user=other, type=Authenticator.Type.WEBAUTHN, data={"name": "x"}
    )
    value = sent(row, other_row)

    response = session.put_json(PASSKEY_PATH, {"id": value, "name": "Renamed"})

    assert response.status_code == 400
    assert [error["message"] for error in response.json()["errors"]] == [
        UNKNOWN_PASSKEY
    ]
    row.refresh_from_db()
    assert row.wrap().name == "Phone"


@pytest.mark.parametrize(
    "sent",
    [
        lambda row, other: [row.pk],
        lambda row, other: [str(row.pk)],
        lambda row, other: [public_passkey_id(other)],
        lambda row, other: [public_passkey_id(row), public_passkey_id(other)],
        lambda row, other: [],
        lambda row, other: public_passkey_id(row),
        lambda row, other: None,
    ],
    ids=["row-id", "row-id-text", "other", "mixed", "empty", "not-a-list", "none"],
)
def test_remove_accepts_only_the_players_public_ids(
    account: "_User", passkey: SoftwarePasskey, sent: Any
) -> None:
    session = _added(BROWSER, account, passkey)
    [row] = _passkeys(account)
    other = UserFactory.create()
    other_row = Authenticator.objects.create(
        user=other, type=Authenticator.Type.WEBAUTHN, data={"name": "x"}
    )
    before = _changes(account)

    response = session.delete_json(
        PASSKEY_PATH, {"authenticators": sent(row, other_row)}
    )

    assert response.status_code == 400
    assert [error["message"] for error in response.json()["errors"]] == [
        UNKNOWN_PASSKEY
    ]
    assert _passkeys(account) == [row]
    assert Authenticator.objects.filter(pk=other_row.pk).exists()
    assert _changes(account) == before


def test_the_authenticator_list_shows_public_ids_only(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    session = _added(base, account, passkey)
    [row] = _passkeys(account)

    response = session.get("account/authenticators")

    assert response.status_code == 200
    data = response.json()["data"]
    [listed] = [item for item in data if item["type"] == "webauthn"]
    assert listed["id"] == public_passkey_id(row)
    assert all(isinstance(value, str) for value in _find_ids(data))


def test_no_row_id_reaches_the_session_records(
    base: str, account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)

    session, login = _passkey_login(base, passkey)
    login_records = _store(session)[AUTHENTICATION_METHODS_SESSION_KEY]
    credential = passkey.sign_in(_request_options(session, REAUTHENTICATE_PATH))
    reauth = session.post(REAUTHENTICATE_PATH, {"credential": credential})
    records = _store(session)[AUTHENTICATION_METHODS_SESSION_KEY]

    for response in (login, reauth):
        assert _find_ids(response.json()["data"]["methods"]) == []
    for record in [*login_records, *records]:
        assert "id" not in record


# Passkey login edge paths.


def test_a_wrong_user_handle_is_refused(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)
    other = UserFactory.create()

    session, response = _passkey_login(BROWSER, passkey, user_handle=user_handle(other))

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


def test_allauths_row_id_handle_is_refused(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)

    _, response = _passkey_login(
        BROWSER, passkey, user_handle=user_pk_to_url_str(account).encode()
    )

    assert response.status_code == 400


def test_a_login_without_a_user_handle_finds_the_account_by_credential(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)

    _, response = _passkey_login(BROWSER, passkey, send_user_handle=False)

    assert response.status_code == 200, response.content
    assert response.json()["meta"]["is_authenticated"] is True


def test_an_unknown_credential_is_refused(account: "_User") -> None:
    stranger = SoftwarePasskey(rp_id=RP_ID, origin=PWA, user_handle=b"made-up")

    _, response = _passkey_login(BROWSER, stranger)

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]


def test_a_login_without_user_verification_is_refused(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)

    _, response = _passkey_login(BROWSER, passkey, user_verified=False)

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]


def test_a_wrong_signature_is_refused(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)
    impostor = SoftwarePasskey(
        rp_id=RP_ID,
        origin=PWA,
        credential_id=passkey.credential_id,
        user_handle=passkey.user_handle,
    )

    _, response = _passkey_login(BROWSER, impostor)

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize(
    "credential",
    [
        {"id": "a", "rawId": "b", "type": "public-key", "response": {}},
        "text",
        [1, 2],
        {"made": "up"},
    ],
)
def test_a_malformed_login_credential_is_refused(credential: object) -> None:
    session = HeadlessSession(BROWSER)
    _request_options(session, LOGIN_PATH)

    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code == 400


def test_a_mismatched_id_is_refused_not_a_server_error(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)
    session = HeadlessSession(BROWSER)
    credential = passkey.sign_in(_request_options(session, LOGIN_PATH))
    credential["id"] = websafe_encode(b"another-made-up-id")

    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]


def test_an_inactive_account_does_not_sign_in(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)
    account.is_active = False
    account.save(update_fields=["is_active"])

    session, response = _passkey_login(BROWSER, passkey)

    assert response.status_code != 200
    assert session.get("auth/session").status_code == 401


@pytest.mark.parametrize("change", ["mismatched", "padded"])
def test_an_odd_registration_id_is_refused_not_a_server_error(
    account: "_User", passkey: SoftwarePasskey, change: str
) -> None:
    session = _signed_in(BROWSER, account)
    credential = passkey.register(_creation_options(session))
    if change == "mismatched":
        credential["id"] = websafe_encode(b"another-made-up-id")
    else:
        credential["id"] = credential["rawId"] = f"{credential['rawId']}="

    response = session.post(
        PASSKEY_PATH, {"name": "Phone", "credential": credential, "passwordless": True}
    )

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]
    assert _passkeys(account) == []


def test_a_credential_id_another_account_holds_is_not_added(
    account: "_User", passkey: SoftwarePasskey
) -> None:
    _added(BROWSER, account, passkey)
    other = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=other, email=other.email, verified=True, primary=True
    )
    copy = SoftwarePasskey(rp_id=RP_ID, origin=PWA, credential_id=passkey.credential_id)
    session = _signed_in(BROWSER, other)

    response = _add(session, copy)

    assert response.status_code == 400
    assert _error_codes(response) == ["incorrect_code"]
    assert _passkeys(other) == []


# Passkey signup stays off (criterion 8) and both clients serve the views
# (criterion 9).


def test_passkey_signup_is_not_served(base: str) -> None:
    client = HeadlessSession(base).client
    path = f"{base}/auth/webauthn/signup"

    assert client.get(path).status_code == 404
    assert client.post(path, {}, content_type="application/json").status_code == 404
    assert client.put(path, {}, content_type="application/json").status_code == 404


def test_the_config_offers_passkey_login(base: str) -> None:
    mfa = HeadlessSession(base).get("config").json()["data"]["mfa"]

    assert mfa == {
        "supported_types": ["totp", "recovery_codes", "webauthn"],
        "passkey_login_enabled": True,
    }


def test_both_clients_serve_the_passkey_views(base: str, account: "_User") -> None:
    session = _signed_in(base, account)

    assert session.get(PASSKEY_PATH).status_code == 200
    assert session.get("account/authenticators").status_code == 200
    assert session.get(REAUTHENTICATE_PATH).status_code == 200
    anonymous = HeadlessSession(base)
    assert anonymous.get(LOGIN_PATH).status_code == 200

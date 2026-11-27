"""A password reset from a key removes the Account's passkeys, audited,
in the reset's transaction (issue #117, ADR 0056). Someone who knew the
password could have added a passkey, which signs in alone; the mailbox
owner's reset must lock them out. A signed-in password change keeps the
passkeys: the player sees and removes them there. Made-up data only."""

from typing import TYPE_CHECKING, Any

import pytest
from allauth.mfa.models import Authenticator

from apps.accounts.tests.factories import enroll_totp
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    NEW_PASSWORD,
    PASSWORD,
    RESET_PREFIX,
    HeadlessSession,
    key_from_link,
    link_in_last_mail,
)
from apps.accounts.tests.passkey_flows import (
    LOGIN_PATH,
    added,
    changes,
    error_codes,
    new_passkey,
    passkeys,
    request_options,
    use_passkey_settings,
    verified_account,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def _reset(base: str, account: "_User") -> Any:
    session = HeadlessSession(base)
    response = session.post("auth/password/request", {"email": account.email})
    assert response.status_code == 200
    key = key_from_link(link_in_last_mail(RESET_PREFIX), RESET_PREFIX)
    return session.post("auth/password/reset", {"key": key, "password": NEW_PASSWORD})


def _codes_exist(account: "_User") -> bool:
    found: bool = Authenticator.objects.filter(
        user_id=account.pk, type=Authenticator.Type.RECOVERY_CODES
    ).exists()
    return found


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_a_reset_removes_a_planted_passkey(base: str) -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    before = changes(account)

    response = _reset(base, account)

    assert response.status_code == 401
    assert passkeys(account) == []
    assert not _codes_exist(account)
    assert changes(account)[len(before) :] == [
        {"change": "removed", "type": "webauthn"},
        {"change": "removed", "type": "recovery_codes"},
    ]
    session = HeadlessSession(BROWSER)
    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    refused = session.post(LOGIN_PATH, {"credential": credential})
    assert refused.status_code == 400
    assert error_codes(refused) == ["incorrect_code"]


def test_a_reset_keeps_totp_and_the_codes() -> None:
    account = verified_account()
    enroll_totp(account)
    Authenticator.objects.create(
        user=account,
        type=Authenticator.Type.WEBAUTHN,
        data={"name": "Phone", "credential": {}},
    )

    response = _reset(BROWSER, account)

    assert response.status_code == 401
    assert passkeys(account) == []
    assert _codes_exist(account)
    assert Authenticator.objects.filter(
        user_id=account.pk, type=Authenticator.Type.TOTP
    ).exists()
    assert changes(account) == [{"change": "removed", "type": "webauthn"}]


def test_a_signed_in_password_change_keeps_the_passkeys() -> None:
    account = verified_account()
    session = added(BROWSER, account, new_passkey())

    response = session.post(
        "account/password/change",
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 200, response.content
    assert len(passkeys(account)) == 1

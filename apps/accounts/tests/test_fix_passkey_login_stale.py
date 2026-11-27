"""Passkey login acts on the Account and passkey as they are under the
Account's row lock, not as they were when the assertion was checked
(issue #117). A passkey removed, or an Account deactivated, between the
check and the login never signs in, and never gives a server error.
Made-up data only."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth import get_user_model

from apps.accounts.passkeys import LoginPasskeyInput
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    LOGIN_PATH,
    added,
    error_codes,
    new_passkey,
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


def _after_check(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[Authenticator], None]
) -> None:
    """Run ``change`` right after the input checked the assertion."""
    original = LoginPasskeyInput.clean_credential

    def checked_then_changed(self: LoginPasskeyInput) -> Authenticator:
        passkey: Authenticator = original(self)
        change(passkey)
        return passkey

    monkeypatch.setattr(LoginPasskeyInput, "clean_credential", checked_then_changed)


def _login(base: str, account: "_User") -> tuple[HeadlessSession, Any]:
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    session = HeadlessSession(base)
    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    return session, credential


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_a_passkey_removed_meanwhile_does_not_sign_in(
    base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = verified_account()
    session, credential = _login(base, account)
    _after_check(
        monkeypatch,
        lambda passkey: Authenticator.objects.filter(pk=passkey.pk).delete(),
    )

    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_an_account_deactivated_meanwhile_does_not_sign_in(
    base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = verified_account()
    session, credential = _login(base, account)
    _after_check(
        monkeypatch,
        lambda passkey: (
            get_user_model().objects.filter(pk=passkey.user_id).update(is_active=False)
        ),
    )

    response = session.post(LOGIN_PATH, {"credential": credential})

    assert response.status_code != 200
    assert session.get("auth/session").status_code == 401

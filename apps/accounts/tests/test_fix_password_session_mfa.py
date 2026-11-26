"""A session that has not passed MFA gets no MFA-protected access.

An enrolled Account's session counts for the API only when it passed the
second factor (a TOTP or recovery code login, an MFA reauthentication,
or the enrollment itself). A password-only session, such as the admin
login's or one made before the Account enrolled, gets 403
``mfa_required``, and cannot change or view the Account's methods after
a password reauthentication: only an MFA reauthentication lets it.
Made-up data only.
"""

from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.test import Client

from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    HeadlessSession,
    browser_client,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

ME = "/api/v1/accounts/me"
TOTP_PATH = "account/authenticators/totp"
CODES_PATH = "account/authenticators/recovery-codes"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


def _verified(**fields: Any) -> "_User":
    account = UserFactory.create(password=PASSWORD, **fields)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def _password_only_session(account: "_User") -> HeadlessSession:
    """A browser session signed in with the password only.

    The admin login once made one (#101 replaced it with allauth's MFA
    login). Django's own password login still makes one, as does a
    session from before the Account enrolled.
    """
    session = HeadlessSession(BROWSER)
    session.client = browser_client()
    assert session.client.login(username=account.email, password=PASSWORD)
    return session


def _get(session: HeadlessSession, path: str) -> Any:
    if session.base == BROWSER:
        return session.client.get(path)
    return session.client.get(path, headers={"X-Session-Token": session.token or ""})


def _mfa_required(response: Any) -> bool:
    return response.status_code == 403 and response.json()["code"] == "mfa_required"


def test_a_password_only_session_is_refused_by_the_api() -> None:
    staff = _verified(is_staff=True)
    session = _password_only_session(staff)

    assert _mfa_required(_get(session, ME))


def test_a_password_only_session_cannot_touch_totp_after_a_password_reauth() -> None:
    staff = _verified(is_staff=True)
    session = _password_only_session(staff)
    assert session.reauthenticate().status_code == 200

    removed = session.delete(TOTP_PATH)
    viewed = session.get(CODES_PATH)
    regenerated = session.post(CODES_PATH, {})

    assert removed.status_code == 401
    assert viewed.status_code == 401
    assert regenerated.status_code == 401
    assert "unused_codes" not in str(viewed.content)
    assert Authenticator.objects.filter(
        user=staff, type=Authenticator.Type.TOTP
    ).exists()


def test_a_password_only_session_acts_after_an_mfa_reauth() -> None:
    staff = _verified(is_staff=True, mfa=False)
    secret = enroll_totp(staff).secret
    session = _password_only_session(staff)

    reauth = session.post("auth/2fa/reauthenticate", {"code": totp_code(secret)})

    assert reauth.status_code == 200
    assert _get(session, ME).status_code == 200
    assert session.get(CODES_PATH).status_code == 200
    assert session.delete(TOTP_PATH).status_code == 200


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_a_session_made_before_enrolling_is_refused_after(base: str) -> None:
    account = _verified()
    session = HeadlessSession(base)
    assert session.login(account.email).status_code == 200
    assert _get(session, ME).status_code == 200

    enroll_totp(account)

    assert _mfa_required(_get(session, ME))


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_the_enrolling_session_keeps_api_access(base: str) -> None:
    staff = _verified(is_staff=True, mfa=False)
    session = HeadlessSession(base)
    session.login(staff.email)
    session.reauthenticate()
    secret = session.get(TOTP_PATH).json()["meta"]["secret"]

    assert session.post(TOTP_PATH, {"code": totp_code(secret)}).status_code == 200
    assert _get(session, ME).status_code == 200


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_a_totp_login_session_is_unaffected(base: str) -> None:
    staff = _verified(is_staff=True, mfa=False)
    secret = enroll_totp(staff).secret
    session = HeadlessSession(base)
    assert session.mfa_login(staff.email, totp_code(secret)).status_code == 200

    assert _get(session, ME).status_code == 200
    session.reauthenticate()
    assert session.get(CODES_PATH).status_code == 200
    assert session.delete(TOTP_PATH).status_code == 200


def test_a_password_session_without_csrf_still_gets_the_csrf_403() -> None:
    staff = _verified(is_staff=True)
    session = _password_only_session(staff)

    response = session.client.post(
        "/api/v1/me/player", {}, content_type="application/json"
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_no_account_still_gets_401() -> None:
    assert Client().get(ME).status_code == 401

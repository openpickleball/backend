"""A password change, a reset, or removing TOTP ends other sessions (#100).

A headless password change records ``password.change`` and ends the
Account's other sessions and app tokens, keeping the one that made it. A
password reset from a key records ``password.change`` and ends every
session. Removing TOTP ends the Account's other sessions. Made-up data
only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.usersessions.models import UserSession
from django.contrib.sessions.models import Session

from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
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
from apps.audit.models import AuditAction, AuditEvent

pytestmark = pytest.mark.django_db

EMAIL = "ending.player@example.org"
TOTP_PATH = "account/authenticators/totp"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


@pytest.fixture
def account() -> Any:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    return account


def _signed_in(base: str) -> HeadlessSession:
    session = HeadlessSession(base)
    assert session.login(EMAIL).status_code == 200
    return session


def _key(session: HeadlessSession) -> str:
    key = session.store().session_key
    assert key
    return key


def _signed_in_now(session: HeadlessSession) -> bool:
    return int(session.get("auth/session").status_code) == 200


def _events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.PASSWORD_CHANGE))


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_password_change_ends_the_other_sessions(base: str, account: Any) -> None:
    current = _signed_in(base)
    others = [_signed_in(APP), _signed_in(BROWSER)]
    other_keys = [_key(other) for other in others]

    response = current.post(
        "account/password/change",
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 200
    assert _signed_in_now(current)
    for other, key in zip(others, other_keys, strict=True):
        assert not _signed_in_now(other)
        assert not Session.objects.filter(session_key=key).exists()
        assert not UserSession.objects.filter(session_key=key).exists()
    assert UserSession.objects.filter(user=account).count() == 1
    [event] = _events()
    assert event.actor_user == account
    assert event.target_object_id == str(account.pk)
    assert event.after == {"method": "change"}


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_session_with_no_row_is_ended_by_the_password_change_too(
    base: str, account: Any
) -> None:
    current = _signed_in(base)
    other = _signed_in(APP)
    UserSession.objects.filter(session_key=_key(other)).delete()

    current.post(
        "account/password/change",
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    # Django's session auth hash no longer matches.
    assert not _signed_in_now(other)


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_refused_change_ends_nothing_and_records_nothing(
    base: str, account: Any
) -> None:
    current = _signed_in(base)
    other = _signed_in(APP)

    response = current.post(
        "account/password/change",
        {"current_password": "not-the-password", "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 400
    assert _signed_in_now(other)
    assert _events() == []


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_password_reset_ends_every_session(base: str, account: Any) -> None:
    signed_in = [_signed_in(APP), _signed_in(BROWSER)]
    keys = [_key(session) for session in signed_in]
    resetting = HeadlessSession(base)
    assert resetting.post("auth/password/request", {"email": EMAIL}).status_code == 200
    key = key_from_link(link_in_last_mail(RESET_PREFIX), RESET_PREFIX)

    response = resetting.post(
        "auth/password/reset", {"key": key, "password": NEW_PASSWORD}
    )

    assert response.status_code == 401  # reset does not sign in
    for session, session_key in zip(signed_in, keys, strict=True):
        assert not _signed_in_now(session)
        assert not Session.objects.filter(session_key=session_key).exists()
    assert not UserSession.objects.filter(user=account).exists()
    [event] = _events()
    assert event.actor_user == account
    assert event.after == {"method": "reset"}


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_removing_totp_ends_the_other_sessions(base: str, account: Any) -> None:
    secret = enroll_totp(account).secret
    current = HeadlessSession(base)
    assert current.mfa_login(EMAIL, totp_code(secret)).status_code == 200
    other = HeadlessSession(APP)
    assert other.mfa_login(EMAIL, totp_code(secret, 1)).status_code == 200
    other_key = _key(other)
    assert current.reauthenticate().status_code == 200

    response = current.delete(TOTP_PATH)

    assert response.status_code == 200
    assert _signed_in_now(current)
    assert not _signed_in_now(other)
    assert not Session.objects.filter(session_key=other_key).exists()


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_refused_totp_removal_ends_nothing(base: str, account: Any) -> None:
    secret = enroll_totp(account).secret
    current = HeadlessSession(base)
    assert current.mfa_login(EMAIL, totp_code(secret)).status_code == 200
    other = HeadlessSession(APP)
    assert other.mfa_login(EMAIL, totp_code(secret, 1)).status_code == 200

    # No recent reauthentication.
    response = current.delete(TOTP_PATH)

    assert response.status_code == 401
    assert _signed_in_now(other)

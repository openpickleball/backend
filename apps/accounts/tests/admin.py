"""Helpers for tests that sign in to the admin (issue #101).

The admin login goes through allauth's headless browser client: the
password, then a TOTP code. ``Client.force_login`` stands in for that
whole login in most tests (the root ``conftest.py`` records the MFA step
for an Account with MFA). These helpers drive the real flow, and age a
session's MFA steps to test the time limits. Made-up data only.
"""

import time
from typing import TYPE_CHECKING, Any

from allauth.account.authentication import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.models import EmailAddress
from django.contrib.sessions.backends.base import SessionBase

from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import BROWSER, PASSWORD, HeadlessSession

if TYPE_CHECKING:
    from django.contrib.auth.models import _User


def verified_account(**fields: Any) -> "_User":
    """An Account with the headless test password and a verified address."""
    account: _User = UserFactory.create(password=PASSWORD, mfa=False, **fields)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def staff_with_totp(**fields: Any) -> tuple["_User", str]:
    """A verified staff Account enrolled in TOTP, and its secret."""
    fields.setdefault("is_staff", True)
    account = verified_account(**fields)
    return account, enroll_totp(account).secret


def sign_in_to_admin(account: "_User", secret: str) -> HeadlessSession:
    """Sign in as the admin login page does: password, then a TOTP code."""
    session = HeadlessSession(BROWSER)
    response = session.mfa_login(account.email, totp_code(secret))
    assert response.status_code == 200, response.content
    return session


def age_mfa_steps(session: SessionBase, seconds: float) -> None:
    """Move every MFA step recorded in ``session`` ``seconds`` into the past."""
    records = session[AUTHENTICATION_METHODS_SESSION_KEY]
    then = time.time() - seconds
    session[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {**record, "at": then} if record.get("method") == "mfa" else record
        for record in records
    ]
    session.save()

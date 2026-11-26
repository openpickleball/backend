"""Every failed login lockout answers 429 (#100 review).

OPEN's login view turned allauth's ``too_many_login_attempts`` into 429,
but the MFA login stage, password reauthentication, and MFA
reauthentication still answered the same ``login_failed`` lockout with
allauth's 400. Each must answer 429 too. Made-up data only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession

pytestmark = pytest.mark.django_db

EMAIL = "lockout.player@example.org"
WRONG = "000000"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


@pytest.fixture
def secret() -> str:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    return enroll_totp(account).secret


def _until_locked(
    session: HeadlessSession, endpoint: str, data: dict[str, str]
) -> list[int]:
    statuses = []
    for _ in range(12):
        status = int(session.post(endpoint, data).status_code)
        statuses.append(status)
        if status != 400:
            break
    return statuses


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_the_mfa_login_stage_lockout_gets_429(base: str, secret: str) -> None:
    session = HeadlessSession(base)
    assert session.login(EMAIL).status_code == 401

    statuses = _until_locked(session, "auth/2fa/authenticate", {"code": WRONG})

    # The lockout (5 failures per 5 minutes), not a later per-view limit.
    assert statuses[-1] == 429, statuses
    assert len(statuses) <= 7, statuses


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_the_password_reauthentication_lockout_gets_429(base: str, secret: str) -> None:
    session = HeadlessSession(base)
    assert session.mfa_login(EMAIL, totp_code(secret)).status_code == 200

    statuses = _until_locked(session, "auth/reauthenticate", {"password": "wrong-one"})

    # The lockout (5 failures per 5 minutes), not a later per-view limit.
    assert statuses[-1] == 429, statuses
    assert len(statuses) <= 7, statuses


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_the_mfa_reauthentication_lockout_gets_429(base: str, secret: str) -> None:
    session = HeadlessSession(base)
    assert session.mfa_login(EMAIL, totp_code(secret)).status_code == 200

    statuses = _until_locked(session, "auth/2fa/reauthenticate", {"code": WRONG})

    # The lockout (5 failures per 5 minutes), not a later per-view limit.
    assert statuses[-1] == 429, statuses
    assert len(statuses) <= 7, statuses

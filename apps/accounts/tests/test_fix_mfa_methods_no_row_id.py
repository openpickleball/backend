"""allauth's ``methods`` payload carries no Authenticator row id.

allauth records an MFA step as ``{"method": "mfa", "id": <row id>, ...}``
and returns the records in every authenticated response. OPEN's API never
exposes a row id (CLAUDE.md, Public identifiers). Made-up data only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


def _mfa_methods(response: Any) -> list[dict[str, Any]]:
    methods: list[dict[str, Any]] = response.json()["data"]["methods"]
    return [method for method in methods if method["method"] == "mfa"]


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_login_reauthentication_and_session_carry_no_row_id(base: str) -> None:
    account = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    enrollment = enroll_totp(account)
    session = HeadlessSession(base)

    login = session.mfa_login(account.email, totp_code(enrollment.secret))
    reauth = session.post(
        "auth/2fa/reauthenticate", {"code": enrollment.recovery_codes[0]}
    )
    current = session.get("auth/session")

    for response in (login, reauth, current):
        assert response.status_code == 200
        methods = _mfa_methods(response)
        assert methods
        for method in methods:
            assert "id" not in method
            assert method["type"] in ("totp", "recovery_codes")

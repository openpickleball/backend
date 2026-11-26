"""TOTP secrets and recovery code seeds are stored encrypted (issue #115,
ADR 0046), through allauth's MFA adapter, and the flows still work.

Made-up Accounts only.
"""

from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator
from django.db import connection

from apps.accounts.mfa_adapter import MFAAdapter
from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.encryption.keys import DecryptionError, is_token

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

TOTP_PATH = "account/authenticators/totp"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


@pytest.fixture(params=[BROWSER, APP], ids=["browser", "app"])
def session(request: pytest.FixtureRequest) -> HeadlessSession:
    return HeadlessSession(request.param)


@pytest.fixture
def account() -> "_User":
    account = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def _raw_data(account: "_User") -> dict[str, str]:
    """Each authenticator's stored JSON, as text, by type."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT type, data::text FROM mfa_authenticator WHERE user_id = %s",
            [account.pk],
        )
        return dict(cursor.fetchall())


def test_the_adapter_is_open_s() -> None:
    assert isinstance(get_adapter(), MFAAdapter)


def test_the_adapter_refuses_plain_text() -> None:
    adapter = MFAAdapter()
    token = adapter.encrypt("JBSWY3DPEHPK3PXP")

    assert is_token(token)
    assert adapter.decrypt(token) == "JBSWY3DPEHPK3PXP"
    with pytest.raises(DecryptionError):
        adapter.decrypt("JBSWY3DPEHPK3PXP")


def test_enrolling_through_the_api_stores_encrypted_secrets(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)
    session.reauthenticate()
    secret = session.get(TOTP_PATH).json()["meta"]["secret"]

    response = session.post(TOTP_PATH, {"code": totp_code(secret)})
    codes = session.get("account/authenticators/recovery-codes").json()["data"]

    assert response.status_code == 200
    raw = _raw_data(account)
    assert set(raw) == {"totp", "recovery_codes"}
    assert secret not in " ".join(raw.values())
    totp = Authenticator.objects.get(user=account, type="totp").data
    seed = Authenticator.objects.get(user=account, type="recovery_codes").data
    assert is_token(totp["secret"])
    assert is_token(seed["seed"])
    for code in codes["unused_codes"]:
        assert code not in raw["recovery_codes"]


def test_login_with_totp_and_a_recovery_code_still_work(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)

    by_totp = session.mfa_login(account.email, totp_code(enrollment.secret))
    session.logout()
    by_code = session.mfa_login(account.email, enrollment.recovery_codes[0])

    assert by_totp.status_code == 200
    assert by_code.status_code == 200

"""TOTP and recovery codes through allauth's headless API (issue #99,
ADR 0043), for both clients.

Made-up Accounts only. Codes come from the secret the API hands out, as
an authenticator app would compute them.
"""

from typing import TYPE_CHECKING

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.core import mail

from apps.accounts.mfa import has_mfa
from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.emails.models import QueuedEmail

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

TOTP_PATH = "account/authenticators/totp"
CODES_PATH = "account/authenticators/recovery-codes"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: pytest.FixtureRequest) -> None:
    """Accept the neighbouring TOTP step, so a test never fails on a
    period boundary between computing a code and posting it."""
    settings.MFA_TOTP_TOLERANCE = 1  # type: ignore[attr-defined]


@pytest.fixture(params=[BROWSER, APP], ids=["browser", "app"])
def session(request: pytest.FixtureRequest) -> HeadlessSession:
    return HeadlessSession(request.param)


@pytest.fixture
def account() -> "_User":
    """A made-up Account with a verified login address."""
    account = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def _flow_ids(response: object) -> set[str]:
    return {flow["id"] for flow in response.json()["data"]["flows"]}  # type: ignore[attr-defined]


def _setup_secret(session: HeadlessSession) -> str:
    response = session.get(TOTP_PATH)
    assert response.status_code == 404
    secret: str = response.json()["meta"]["secret"]
    return secret


def test_totp_url_names_open_as_issuer(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)

    meta = session.get(TOTP_PATH).json()["meta"]

    assert "issuer=OPEN" in meta["totp_url"]
    assert "testserver" not in meta["totp_url"]


def test_enrolling_needs_a_recent_reauthentication(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)
    secret = _setup_secret(session)

    response = session.post(TOTP_PATH, {"code": totp_code(secret)})

    assert response.status_code == 401
    assert "reauthenticate" in _flow_ids(response)
    assert not Authenticator.objects.filter(user=account).exists()


def test_enrolling_after_reauthentication_adds_totp_and_recovery_codes(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)
    session.reauthenticate()
    secret = _setup_secret(session)

    response = session.post(TOTP_PATH, {"code": totp_code(secret)})

    assert response.status_code == 200
    assert response.json()["data"]["type"] == "totp"
    assert has_mfa(account)
    assert Authenticator.objects.filter(
        user=account, type=Authenticator.Type.RECOVERY_CODES
    ).exists()


def test_a_wrong_code_does_not_enroll(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)
    session.reauthenticate()
    secret = _setup_secret(session)
    wrong = f"{(int(totp_code(secret)) + 1) % 1_000_000:06d}"

    response = session.post(TOTP_PATH, {"code": wrong})

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "incorrect_code"
    assert not Authenticator.objects.filter(user=account).exists()


def test_recovery_codes_are_shown_once(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)
    session.reauthenticate()
    session.post(TOTP_PATH, {"code": totp_code(_setup_secret(session))})

    first = session.get(CODES_PATH)
    second = session.get(CODES_PATH)

    assert first.status_code == 200
    assert len(first.json()["data"]["unused_codes"]) == 10
    assert second.status_code == 200
    assert "unused_codes" not in second.json()["data"]


def test_login_with_totp(session: HeadlessSession, account: "_User") -> None:
    secret = enroll_totp(account).secret

    response = session.mfa_login(account.email, totp_code(secret))

    assert response.status_code == 200
    assert response.json()["meta"]["is_authenticated"] is True


def test_login_stops_at_the_mfa_stage(
    session: HeadlessSession, account: "_User"
) -> None:
    enroll_totp(account)

    response = session.login(account.email)

    assert response.status_code == 401
    assert "mfa_authenticate" in _flow_ids(response)
    assert session.get("auth/session").status_code == 401


def test_a_wrong_code_does_not_log_in(
    session: HeadlessSession, account: "_User"
) -> None:
    secret = enroll_totp(account).secret
    wrong = f"{(int(totp_code(secret)) + 1) % 1_000_000:06d}"

    response = session.mfa_login(account.email, wrong)

    assert response.status_code == 400
    assert session.get("auth/session").status_code == 401


def test_login_with_a_recovery_code_works_once(
    session: HeadlessSession, account: "_User"
) -> None:
    code = enroll_totp(account).recovery_codes[0]

    first = session.mfa_login(account.email, code)
    session.logout()
    again = session.mfa_login(account.email, code)

    assert first.status_code == 200
    assert again.status_code == 400
    assert again.json()["errors"][0]["code"] == "incorrect_code"
    assert session.get("auth/session").status_code == 401


def test_removing_totp_needs_a_recent_reauthentication(
    session: HeadlessSession, account: "_User"
) -> None:
    secret = enroll_totp(account).secret
    session.mfa_login(account.email, totp_code(secret))

    response = session.delete(TOTP_PATH)

    assert response.status_code == 401
    assert "reauthenticate" in _flow_ids(response)
    assert has_mfa(account)


def test_removing_totp_after_reauthentication_removes_the_codes_too(
    session: HeadlessSession, account: "_User"
) -> None:
    secret = enroll_totp(account).secret
    session.mfa_login(account.email, totp_code(secret))
    session.reauthenticate()

    response = session.delete(TOTP_PATH)

    assert response.status_code == 200
    assert not Authenticator.objects.filter(user=account).exists()


def test_regenerating_codes_needs_a_recent_reauthentication(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, totp_code(enrollment.secret))

    response = session.post(CODES_PATH, {})

    assert response.status_code == 401
    assert "reauthenticate" in _flow_ids(response)
    assert enrollment.recovery_codes[1] in (
        Authenticator.objects.get(user=account, type=Authenticator.Type.RECOVERY_CODES)
        .wrap()
        .get_unused_codes()
    )


def test_regenerating_codes_replaces_the_old_ones(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, totp_code(enrollment.secret))
    session.reauthenticate()

    response = session.post(CODES_PATH, {})

    assert response.status_code == 200
    new_codes = response.json()["data"]["unused_codes"]
    assert len(new_codes) == 10
    assert not set(new_codes) & set(enrollment.recovery_codes)


def test_reauthenticating_with_a_code(
    session: HeadlessSession, account: "_User"
) -> None:
    secret = enroll_totp(account).secret
    session.mfa_login(account.email, totp_code(secret))

    response = session.post("auth/2fa/reauthenticate", {"code": totp_code(secret, 1)})

    assert response.status_code == 200


def test_mfa_changes_send_no_email(session: HeadlessSession, account: "_User") -> None:
    session.login(account.email)
    session.reauthenticate()
    session.post(TOTP_PATH, {"code": totp_code(_setup_secret(session))})
    session.post(CODES_PATH, {})
    session.delete(TOTP_PATH)

    assert not Authenticator.objects.filter(user=account).exists()
    assert mail.outbox == []
    assert not QueuedEmail.objects.exists()


def test_adding_an_address_is_blocked_while_mfa_is_on(
    session: HeadlessSession, account: "_User"
) -> None:
    secret = enroll_totp(account).secret
    session.mfa_login(account.email, totp_code(secret))

    response = session.post("account/email", {"email": "another.made.up@example.org"})

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "add_email_blocked"
    assert not EmailAddress.objects.filter(email="another.made.up@example.org").exists()

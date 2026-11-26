"""Each MFA change writes one ``mfa.change`` Audit Event (issue #99,
ADR 0043), for both clients, holding no secret and no code."""

import json
from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.contrib.contenttypes.models import ContentType

from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.audit.models import AuditAction, AuditEvent

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

TOTP_PATH = "account/authenticators/totp"
CODES_PATH = "account/authenticators/recovery-codes"


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


def _events(account: "_User") -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.MFA_CHANGE).order_by("pk"))


def _changes(account: "_User") -> list[dict[str, str]]:
    events = _events(account)
    for event in events:
        assert event.actor_user_id == account.pk
        assert event.target_content_type == ContentType.objects.get_for_model(
            type(account)
        )
        assert event.target_object_id == str(account.pk)
        assert event.before is None
        assert event.reason == ""
    return [event.after for event in events]  # type: ignore[misc]


def _assert_holds_none_of(account: "_User", secrets: list[str]) -> None:
    for event in _events(account):
        stored = json.dumps([event.before, event.after, event.reason])
        for secret in secrets:
            assert secret not in stored


def _setup_secret(session: HeadlessSession) -> str:
    secret: str = session.get(TOTP_PATH).json()["meta"]["secret"]
    return secret


def _codes(account: "_User") -> list[str]:
    codes: list[str] = (
        Authenticator.objects.get(user=account, type=Authenticator.Type.RECOVERY_CODES)
        .wrap()
        .generate_codes()
    )
    return codes


def test_enrolling_records_totp_and_recovery_codes_added(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)
    session.reauthenticate()
    secret = _setup_secret(session)

    session.post(TOTP_PATH, {"code": totp_code(secret)})

    assert _changes(account) == [
        {"change": "added", "type": "totp"},
        {"change": "added", "type": "recovery_codes"},
    ]
    _assert_holds_none_of(account, [secret, *_codes(account)])


def test_enrolling_without_reauthentication_records_nothing(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)
    secret = _setup_secret(session)

    response = session.post(TOTP_PATH, {"code": totp_code(secret)})

    assert response.status_code == 401
    assert _events(account) == []


def test_a_wrong_enrollment_code_records_nothing(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)
    session.reauthenticate()
    secret = _setup_secret(session)
    wrong = f"{(int(totp_code(secret)) + 1) % 1_000_000:06d}"

    assert session.post(TOTP_PATH, {"code": wrong}).status_code == 400
    assert _events(account) == []


def test_removing_totp_records_both_removals(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, totp_code(enrollment.secret))
    session.reauthenticate()

    assert session.delete(TOTP_PATH).status_code == 200
    assert _changes(account) == [
        {"change": "removed", "type": "totp"},
        {"change": "removed", "type": "recovery_codes"},
    ]
    _assert_holds_none_of(account, [enrollment.secret, *enrollment.recovery_codes])


def test_removing_totp_without_reauthentication_records_nothing(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, totp_code(enrollment.secret))

    assert session.delete(TOTP_PATH).status_code == 401
    assert _events(account) == []
    assert Authenticator.objects.filter(user=account).count() == 2


def test_removing_totp_without_one_records_nothing(
    session: HeadlessSession, account: "_User"
) -> None:
    session.login(account.email)
    session.reauthenticate()

    assert session.delete(TOTP_PATH).status_code == 200
    assert _events(account) == []


def test_regenerating_codes_records_one_event(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, totp_code(enrollment.secret))
    session.reauthenticate()

    response = session.post(CODES_PATH, {})

    assert response.status_code == 200
    assert _changes(account) == [{"change": "regenerated", "type": "recovery_codes"}]
    _assert_holds_none_of(
        account,
        [enrollment.secret, *enrollment.recovery_codes, *_codes(account)],
    )


def test_regenerating_without_reauthentication_records_nothing(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, totp_code(enrollment.secret))

    assert session.post(CODES_PATH, {}).status_code == 401
    assert _events(account) == []


def test_logging_in_with_a_recovery_code_records_its_use(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)

    response = session.mfa_login(account.email, enrollment.recovery_codes[0])

    assert response.status_code == 200
    assert _changes(account) == [{"change": "used", "type": "recovery_codes"}]
    _assert_holds_none_of(account, enrollment.recovery_codes)


def test_logging_in_with_totp_records_nothing(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)

    assert (
        session.mfa_login(account.email, totp_code(enrollment.secret)).status_code
        == 200
    )
    assert _events(account) == []


def test_a_reused_recovery_code_records_nothing(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, enrollment.recovery_codes[0])
    session.logout()

    again = session.mfa_login(account.email, enrollment.recovery_codes[0])

    assert again.status_code == 400
    assert len(_events(account)) == 1


def test_reauthenticating_with_a_recovery_code_records_its_use(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, totp_code(enrollment.secret))

    response = session.post(
        "auth/2fa/reauthenticate", {"code": enrollment.recovery_codes[3]}
    )

    assert response.status_code == 200
    assert _changes(account) == [{"change": "used", "type": "recovery_codes"}]


def test_reauthenticating_with_a_wrong_code_records_nothing(
    session: HeadlessSession, account: "_User"
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, totp_code(enrollment.secret))

    response = session.post("auth/2fa/reauthenticate", {"code": "00000000"})

    assert response.status_code == 400
    assert _events(account) == []

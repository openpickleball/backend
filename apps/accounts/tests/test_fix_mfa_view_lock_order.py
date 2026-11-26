"""OPEN's MFA views lock the Account row first (one lock order).

Two concurrent TOTP removals each recorded their removals, and a recovery
code login (codes row, then Account row) could deadlock with a
regeneration (Account row, then codes row). Every OPEN MFA view takes the
Account row ``FOR UPDATE`` before it reads an authenticator, so changes
to one Account's methods run in turn. Made-up data only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.audit.models import AuditAction, AuditEvent

pytestmark = pytest.mark.django_db

TOTP_PATH = "account/authenticators/totp"
CODES_PATH = "account/authenticators/recovery-codes"
ACCOUNT_TABLE = f'"{User._meta.db_table}"'
AUTHENTICATOR_TABLE = '"mfa_authenticator"'


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


@pytest.fixture(params=[BROWSER, APP], ids=["browser", "app"])
def session(request: pytest.FixtureRequest) -> HeadlessSession:
    return HeadlessSession(request.param)


@pytest.fixture
def account() -> User:
    account: User = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def _assert_account_locked_first(queries: CaptureQueriesContext) -> None:
    sql = [query["sql"] for query in queries.captured_queries]
    locks = [
        index
        for index, text in enumerate(sql)
        if f"FROM {ACCOUNT_TABLE}" in text and "FOR UPDATE" in text
    ]
    reads = [index for index, text in enumerate(sql) if AUTHENTICATOR_TABLE in text]
    assert locks, sql
    assert reads, sql
    assert locks[0] < reads[0], sql


def _call(session: HeadlessSession, method: str, path: str) -> Any:
    with CaptureQueriesContext(connection) as queries:
        if method == "GET":
            response = session.get(path)
        elif method == "DELETE":
            response = session.delete(path)
        else:
            response = session.post(path, {})
    assert response.status_code == 200, response.content
    _assert_account_locked_first(queries)
    return response


def test_totp_removal_locks_the_account_first(
    session: HeadlessSession, account: User
) -> None:
    secret = enroll_totp(account).secret
    session.mfa_login(account.email, totp_code(secret))
    session.reauthenticate()

    _call(session, "DELETE", TOTP_PATH)


def test_recovery_codes_lock_the_account_first(
    session: HeadlessSession, account: User
) -> None:
    secret = enroll_totp(account).secret
    session.mfa_login(account.email, totp_code(secret))
    session.reauthenticate()

    _call(session, "GET", CODES_PATH)
    _call(session, "POST", CODES_PATH)


def test_totp_enrollment_locks_the_account_first(
    session: HeadlessSession, account: User
) -> None:
    session.login(account.email)
    session.reauthenticate()
    secret = session.get(TOTP_PATH).json()["meta"]["secret"]

    with CaptureQueriesContext(connection) as queries:
        response = session.post(TOTP_PATH, {"code": totp_code(secret)})

    assert response.status_code == 200
    _assert_account_locked_first(queries)


def test_a_recovery_code_login_locks_the_account_first(
    session: HeadlessSession, account: User
) -> None:
    codes = enroll_totp(account).recovery_codes
    session.login(account.email)

    with CaptureQueriesContext(connection) as queries:
        response = session.post("auth/2fa/authenticate", {"code": codes[0]})

    assert response.status_code == 200
    _assert_account_locked_first(queries)


def test_an_mfa_reauthentication_locks_the_account_first(
    session: HeadlessSession, account: User
) -> None:
    enrollment = enroll_totp(account)
    session.mfa_login(account.email, totp_code(enrollment.secret))

    with CaptureQueriesContext(connection) as queries:
        response = session.post(
            "auth/2fa/reauthenticate", {"code": enrollment.recovery_codes[0]}
        )

    assert response.status_code == 200
    _assert_account_locked_first(queries)


def test_a_second_totp_removal_records_nothing(
    session: HeadlessSession, account: User
) -> None:
    secret = enroll_totp(account).secret
    session.mfa_login(account.email, totp_code(secret))
    session.reauthenticate()
    assert session.delete(TOTP_PATH).status_code == 200
    events = AuditEvent.objects.filter(action=AuditAction.MFA_CHANGE).count()

    again = session.delete(TOTP_PATH)

    assert again.status_code in (200, 401)
    assert AuditEvent.objects.filter(action=AuditAction.MFA_CHANGE).count() == events

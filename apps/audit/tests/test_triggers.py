"""The database refuses UPDATE, DELETE, and TRUNCATE on audit events.

These run raw SQL as the test database's role, which skips the ORM guards,
so only the append-only trigger (migration 0002) can stop them.
"""

import pytest
from django.db import ProgrammingError, connection, transaction

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event

pytestmark = pytest.mark.django_db

TABLE = AuditEvent._meta.db_table


@pytest.fixture
def event() -> AuditEvent:
    return record_event(
        action=AuditAction.DISPUTE_OUTCOME,
        target=UserFactory.create(),
        reason="Made-up original reason.",
    )


def _execute_refused(sql: str) -> ProgrammingError:
    """Run ``sql`` in a savepoint and return the error the trigger raised."""
    with (
        pytest.raises(ProgrammingError) as excinfo,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(sql)
    # The exact class, not a subclass: psycopg's InsufficientPrivilege
    # (SQLSTATE 42501) is wrapped as Django's ProgrammingError.
    assert type(excinfo.value) is ProgrammingError
    assert "append-only" in str(excinfo.value)
    return excinfo.value


def _assert_unchanged(event: AuditEvent) -> None:
    assert AuditEvent.objects.count() == 1
    assert AuditEvent.objects.get(pk=event.pk).reason == "Made-up original reason."


def test_trigger_refuses_raw_update(event: AuditEvent) -> None:
    error = _execute_refused(f"UPDATE {TABLE} SET reason = 'changed'")

    assert "UPDATE" in str(error)
    _assert_unchanged(event)


def test_trigger_refuses_raw_delete(event: AuditEvent) -> None:
    error = _execute_refused(f"DELETE FROM {TABLE}")

    assert "DELETE" in str(error)
    _assert_unchanged(event)


def test_trigger_refuses_truncate_of_a_non_empty_table(event: AuditEvent) -> None:
    # The row was inserted in this test's transaction, so its deferred
    # foreign key checks are still pending, and Postgres refuses TRUNCATE
    # for that reason before the audit trigger runs. Run them now, so the
    # TRUNCATE below reaches the trigger.
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    error = _execute_refused(f"TRUNCATE {TABLE}")

    assert "TRUNCATE" in str(error)
    _assert_unchanged(event)


def test_trigger_allows_truncate_of_an_empty_table() -> None:
    assert not AuditEvent.objects.exists()

    with connection.cursor() as cursor:
        cursor.execute(f"TRUNCATE {TABLE}")

    assert not AuditEvent.objects.exists()


def test_trigger_allows_insert() -> None:
    record_event(action=AuditAction.ADMIN_ADD, target=UserFactory.create())

    assert AuditEvent.objects.count() == 1


def test_both_triggers_exist() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass "
            "AND NOT tgisinternal ORDER BY tgname",
            [TABLE],
        )
        names = [row[0] for row in cursor.fetchall()]

    assert names == [
        "audit_auditevent_no_truncate",
        "audit_auditevent_no_update_delete",
    ]


@pytest.mark.django_db(transaction=True)
def test_flush_still_works_when_no_audit_event_exists() -> None:
    # A transactional test flushes every table (TRUNCATE) at teardown. This
    # one creates an Account but no audit event, so the flush must succeed.
    UserFactory.create()

    assert not AuditEvent.objects.exists()


def test_trigger_function_turns_row_security_off() -> None:
    # The emptiness check must see every row: a SELECT policy must make it
    # fail, not hide rows from it (TRUNCATE itself ignores policies).
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proconfig FROM pg_proc WHERE proname = %s",
            ["audit_auditevent_append_only"],
        )
        (config,) = cursor.fetchone() or (None,)

    assert "row_security=off" in (config or [])

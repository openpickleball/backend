"""TRUNCATE cannot get around the append-only trigger.

Review round 1: the empty-table check must look at the table that fired the
trigger (a temp table of the same name must not shadow it), and an empty
TRUNCATE is allowed only under READ COMMITTED, since an older snapshot can
miss a row another session committed.
"""

import pytest
from django.db import ProgrammingError, connection, transaction

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event

TABLE = AuditEvent._meta.db_table


def _run_in_one_transaction(*statements: str) -> None:
    """Run ``statements`` in order inside one atomic block (a savepoint)."""
    with transaction.atomic(), connection.cursor() as cursor:
        for sql in statements:
            cursor.execute(sql)


@pytest.mark.django_db
def test_temp_table_of_the_same_name_does_not_shadow_the_check() -> None:
    record_event(action=AuditAction.ADMIN_ADD, target=UserFactory.create())
    with connection.cursor() as cursor:
        # Run the row's deferred foreign key checks, so TRUNCATE is refused
        # by the trigger and not for pending trigger events.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    with pytest.raises(ProgrammingError, match="append-only"):
        _run_in_one_transaction(
            f"CREATE TEMP TABLE {TABLE} (x int)", f"TRUNCATE public.{TABLE}"
        )

    assert AuditEvent.objects.count() == 1


@pytest.mark.django_db
def test_trigger_function_pins_its_search_path() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proconfig FROM pg_proc WHERE proname = %s",
            ["audit_auditevent_append_only"],
        )
        (config,) = cursor.fetchone() or (None,)

    assert config == ["search_path=pg_catalog, pg_temp"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_empty_truncate_is_refused_outside_read_committed(level: str) -> None:
    assert not AuditEvent.objects.exists()

    with pytest.raises(ProgrammingError, match="append-only"):
        _run_in_one_transaction(
            f"SET TRANSACTION ISOLATION LEVEL {level}", f"TRUNCATE {TABLE}"
        )


@pytest.mark.django_db(transaction=True)
def test_empty_truncate_is_allowed_under_read_committed() -> None:
    _run_in_one_transaction(
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED", f"TRUNCATE {TABLE}"
    )

    assert not AuditEvent.objects.exists()

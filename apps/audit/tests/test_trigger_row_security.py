"""Row-level security cannot hide rows from the TRUNCATE emptiness check.

Review round 2: the trigger's check ran as the caller, so a SELECT policy
that hid every row made a populated table look empty, while TRUNCATE itself
ignores row-level security. The function must run its check with
row_security off, so a caller subject to a policy gets an error instead.
"""

import uuid

import pytest
from django.db import ProgrammingError, connection, transaction

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event

pytestmark = pytest.mark.django_db

TABLE = AuditEvent._meta.db_table


def _truncate_as_role_hidden_by_policy() -> None:
    """In one savepoint: make a role that a policy hides every row from,
    switch to it, and TRUNCATE. The rollback drops the role and policy.
    """
    role = f"audit_rls_probe_{uuid.uuid4().hex[:12]}"
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {role} NOLOGIN")
        cursor.execute(f"GRANT SELECT, TRUNCATE ON {TABLE} TO {role}")
        cursor.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        cursor.execute(
            f"CREATE POLICY hide_all ON {TABLE} FOR SELECT TO {role} USING (false)"
        )
        cursor.execute(f"SET LOCAL ROLE {role}")
        cursor.execute(f"TRUNCATE {TABLE}")


def test_truncate_is_refused_when_a_policy_hides_every_row() -> None:
    event = record_event(
        action=AuditAction.ADMIN_ADD,
        target=UserFactory.create(),
        reason="Made-up original reason.",
    )
    with connection.cursor() as cursor:
        # Run the row's deferred foreign key checks, so TRUNCATE reaches the
        # trigger instead of failing for pending trigger events.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    with pytest.raises(ProgrammingError):
        _truncate_as_role_hidden_by_policy()

    assert AuditEvent.objects.get(pk=event.pk).reason == "Made-up original reason."
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user = session_user")
        assert cursor.fetchone() == (True,)

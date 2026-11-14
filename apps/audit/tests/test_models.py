"""The ORM refuses to change or delete an AuditEvent before any SQL runs."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent, AuditEventImmutable
from apps.audit.services import record_event

pytestmark = pytest.mark.django_db


@pytest.fixture
def event() -> AuditEvent:
    return record_event(
        action=AuditAction.ADMIN_CHANGE,
        target=UserFactory.create(),
        reason="Made-up original reason.",
    )


def _assert_unchanged(event: AuditEvent) -> None:
    stored = AuditEvent.objects.get(pk=event.pk)
    assert stored.reason == "Made-up original reason."
    assert AuditEvent.objects.count() == 1


def test_audit_event_immutable_is_a_runtime_error() -> None:
    assert issubclass(AuditEventImmutable, RuntimeError)


def test_save_on_an_existing_event_is_refused(event: AuditEvent) -> None:
    event.reason = "Changed."

    with (
        CaptureQueriesContext(connection) as queries,
        pytest.raises(AuditEventImmutable),
    ):
        event.save()

    assert len(queries) == 0
    _assert_unchanged(event)


def test_instance_delete_is_refused(event: AuditEvent) -> None:
    with (
        CaptureQueriesContext(connection) as queries,
        pytest.raises(AuditEventImmutable),
    ):
        event.delete()

    assert len(queries) == 0
    _assert_unchanged(event)


def test_queryset_update_is_refused(event: AuditEvent) -> None:
    with (
        CaptureQueriesContext(connection) as queries,
        pytest.raises(AuditEventImmutable),
    ):
        AuditEvent.objects.filter(pk=event.pk).update(reason="Changed.")

    assert len(queries) == 0
    _assert_unchanged(event)


def test_queryset_delete_is_refused(event: AuditEvent) -> None:
    with (
        CaptureQueriesContext(connection) as queries,
        pytest.raises(AuditEventImmutable),
    ):
        AuditEvent.objects.all().delete()

    assert len(queries) == 0
    _assert_unchanged(event)


def test_bulk_update_is_refused(event: AuditEvent) -> None:
    event.reason = "Changed."

    with (
        CaptureQueriesContext(connection) as queries,
        pytest.raises(AuditEventImmutable),
    ):
        AuditEvent.objects.bulk_update([event], ["reason"])

    assert len(queries) == 0
    _assert_unchanged(event)


def test_a_new_event_can_still_be_saved() -> None:
    target = UserFactory.create()

    event = record_event(action=AuditAction.ADMIN_ADD, target=target)

    assert event.pk is not None

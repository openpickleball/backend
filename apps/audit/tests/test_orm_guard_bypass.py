"""The ORM guards cover every write path, not only save() and objects.

Review round 1 found paths that reached SQL and were stopped only by the
database trigger: a new instance given an existing pk, force_update and
update_fields, the base manager, bulk_create with update_conflicts, and
deserialized saves (loaddata), which call save_base() directly.
"""

import json

import pytest
from django.core import serializers
from pytest_django import DjangoAssertNumQueries

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


def _copy(event: AuditEvent, **changes: object) -> AuditEvent:
    """A new, unsaved instance with ``event``'s field values."""
    values = {
        "action": event.action,
        "target_content_type_id": event.target_content_type_id,
        "target_object_id": event.target_object_id,
        "reason": "Changed.",
    }
    values.update(changes)
    return AuditEvent(**values)


def _assert_unchanged(event: AuditEvent) -> None:
    assert AuditEvent.objects.count() == 1
    assert AuditEvent.objects.get(pk=event.pk).reason == "Made-up original reason."


def test_new_instance_with_an_existing_pk_is_refused(
    event: AuditEvent, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    forged = _copy(event, pk=event.pk)

    with django_assert_num_queries(0), pytest.raises(AuditEventImmutable):
        forged.save()

    _assert_unchanged(event)


def test_force_update_is_refused(
    event: AuditEvent, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    forged = _copy(event)

    with django_assert_num_queries(0), pytest.raises(AuditEventImmutable):
        forged.save(force_update=True)

    _assert_unchanged(event)


def test_update_fields_is_refused(
    event: AuditEvent, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    forged = _copy(event)

    with django_assert_num_queries(0), pytest.raises(AuditEventImmutable):
        forged.save(update_fields=["reason"])

    _assert_unchanged(event)


def test_base_manager_update_is_refused(
    event: AuditEvent, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    with django_assert_num_queries(0), pytest.raises(AuditEventImmutable):
        AuditEvent._base_manager.filter(pk=event.pk).update(reason="Changed.")

    _assert_unchanged(event)


def test_base_manager_delete_is_refused(
    event: AuditEvent, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    with django_assert_num_queries(0), pytest.raises(AuditEventImmutable):
        AuditEvent._base_manager.filter(pk=event.pk).delete()

    _assert_unchanged(event)


def test_bulk_create_with_update_conflicts_is_refused(
    event: AuditEvent, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    forged = _copy(event, pk=event.pk)

    with django_assert_num_queries(0), pytest.raises(AuditEventImmutable):
        AuditEvent.objects.bulk_create(
            [forged],
            update_conflicts=True,
            unique_fields=["id"],
            update_fields=["reason"],
        )

    _assert_unchanged(event)


def test_deserialized_save_is_refused(
    event: AuditEvent, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    data = json.loads(serializers.serialize("json", [event]))
    data[0]["fields"]["reason"] = "Changed."
    (deserialized,) = serializers.deserialize("json", json.dumps(data))

    with django_assert_num_queries(0), pytest.raises(AuditEventImmutable):
        deserialized.save()

    _assert_unchanged(event)


def test_plain_bulk_create_still_inserts() -> None:
    target = UserFactory.create()
    new = AuditEvent(
        action=AuditAction.ADMIN_ADD,
        target_content_type_id=record_event(
            action=AuditAction.ADMIN_ADD, target=target
        ).target_content_type_id,
        target_object_id=str(target.pk),
    )

    AuditEvent.objects.bulk_create([new])

    assert AuditEvent.objects.count() == 2

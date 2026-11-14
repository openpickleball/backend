"""snapshot() gives a many-to-many field as JSON-safe primary keys.

Review round 1: snapshot(account, ["groups"]) returned Group instances, and
record_event then failed with TypeError when encoding them.
"""

import pytest
from django.contrib.auth.models import Group

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event, snapshot

pytestmark = pytest.mark.django_db


def test_many_to_many_gives_sorted_primary_keys() -> None:
    account = UserFactory.create()
    second = Group.objects.create(name="Made-up group B")
    first = Group.objects.create(name="Made-up group A")
    account.groups.add(second, first)

    assert snapshot(account, ["groups"]) == {"groups": sorted([first.pk, second.pk])}


def test_empty_many_to_many_gives_an_empty_list() -> None:
    account = UserFactory.create()

    assert snapshot(account, ["groups"]) == {"groups": []}


def test_many_to_many_snapshot_can_be_recorded() -> None:
    account = UserFactory.create()
    group = Group.objects.create(name="Made-up moderators")
    before = snapshot(account, ["groups"])
    account.groups.add(group)

    event = record_event(
        action=AuditAction.ADMIN_CHANGE,
        target=account,
        before=before,
        after=snapshot(account, ["groups"]),
    )

    stored = AuditEvent.objects.get(pk=event.pk)
    assert stored.before == {"groups": []}
    assert stored.after == {"groups": [group.pk]}

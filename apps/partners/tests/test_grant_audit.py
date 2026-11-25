"""Organizer Application Grant changes are audited (#158).

Each add, change, and removal records one Audit Event with the acting
Account, holding the Organizer's OPEN Code and the application's client
id, never an email, an Organizer's name, or the application's name.
Made-up data only.
"""

import json
from typing import TYPE_CHECKING

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerFactory
from apps.partners.models import OrganizerApplicationGrant
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    PartnerApplicationFactory,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

GRANT_ACTIONS = [
    AuditAction.ORGANIZER_APPLICATION_GRANT_ADD,
    AuditAction.ORGANIZER_APPLICATION_GRANT_CHANGE,
    AuditAction.ORGANIZER_APPLICATION_GRANT_REMOVE,
]


@pytest.fixture
def superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


@pytest.fixture
def admin_client(client: Client, superuser: "_User") -> Client:
    client.force_login(superuser)
    return client


def _grant_events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action__in=GRANT_ACTIONS).order_by("id"))


def test_grant_admin_add_change_and_delete_each_record_one_event(
    admin_client: Client, superuser: "_User"
) -> None:
    organizer = OrganizerFactory.create()
    application = PartnerApplicationFactory.create()
    other = PartnerApplicationFactory.create()

    added = admin_client.post(
        reverse("admin:partners_organizerapplicationgrant_add"),
        {"organizer": str(organizer.pk), "application": str(application.pk)},
    )
    assert added.status_code == 302
    grant = OrganizerApplicationGrant.objects.get()
    changed = admin_client.post(
        reverse("admin:partners_organizerapplicationgrant_change", args=[grant.pk]),
        {"organizer": str(organizer.pk), "application": str(other.pk)},
    )
    assert changed.status_code == 302
    deleted = admin_client.post(
        reverse("admin:partners_organizerapplicationgrant_delete", args=[grant.pk]),
        {"post": "yes"},
    )

    assert deleted.status_code == 302
    first = {"organizer": organizer.public_code, "application": application.client_id}
    second = {"organizer": organizer.public_code, "application": other.client_id}
    events = _grant_events()
    assert [(e.action, e.before, e.after) for e in events] == [
        (AuditAction.ORGANIZER_APPLICATION_GRANT_ADD, None, first),
        (AuditAction.ORGANIZER_APPLICATION_GRANT_CHANGE, first, second),
        (AuditAction.ORGANIZER_APPLICATION_GRANT_REMOVE, second, None),
    ]
    assert {e.actor_user for e in events} == {superuser}
    stored = json.dumps([[e.before, e.after] for e in events])
    for value in (organizer.name, application.name, other.name):
        assert value not in stored


def test_a_grant_saved_unchanged_records_nothing() -> None:
    grant = OrganizerApplicationGrantFactory.create()
    before = len(_grant_events())

    grant.save()

    assert len(_grant_events()) == before

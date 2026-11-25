"""Deleting a Partner Application in the admin records its grants (#158).

The delete view and ``delete_selected`` record one removal for each
Organizer Application Grant the delete cascades to, before the delete, with
the admin as actor. Made-up data only.
"""

from typing import TYPE_CHECKING

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.partners.tests.factories import OrganizerApplicationGrantFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


@pytest.fixture
def superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


@pytest.fixture
def admin_client(client: Client, superuser: "_User") -> Client:
    client.force_login(superuser)
    return client


def _removals() -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(
            action=AuditAction.ORGANIZER_APPLICATION_GRANT_REMOVE
        ).order_by("id")
    )


def test_deleting_an_application_records_its_grants_removed(
    admin_client: Client, superuser: "_User"
) -> None:
    grant = OrganizerApplicationGrantFactory.create()
    application = grant.application

    response = admin_client.post(
        reverse("admin:partners_partnerapplication_delete", args=[application.pk]),
        {"post": "yes"},
    )

    assert response.status_code == 302
    [event] = _removals()
    assert event.target_object_id == str(grant.pk)
    assert event.actor_user == superuser
    assert event.before == {
        "organizer": grant.organizer.public_code,
        "application": application.client_id,
    }
    assert event.after is None


def test_delete_selected_applications_records_each_grant(
    admin_client: Client, superuser: "_User"
) -> None:
    grants = OrganizerApplicationGrantFactory.create_batch(2)

    response = admin_client.post(
        reverse("admin:partners_partnerapplication_changelist"),
        {
            "action": "delete_selected",
            "_selected_action": [str(g.application_id) for g in grants],
            "post": "yes",
        },
    )

    assert response.status_code == 302
    events = _removals()
    assert sorted(e.target_object_id for e in events) == sorted(
        str(g.pk) for g in grants
    )
    assert {e.actor_user for e in events} == {superuser}

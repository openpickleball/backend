"""An Account that deletes itself still loses its roles and grants (#158).

A removal recorded during the Account's own delete cannot name the
Account as actor: the row is gone at commit, and the audit foreign key
would refuse it. The event is recorded with no actor instead, and still
holds the Account's row id. Made-up data only.
"""

from typing import TYPE_CHECKING

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.actor import acting_as
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import ClubRoleFactory
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    PartnerApplicationFactory,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


def test_an_account_deleting_itself_records_its_role_removed_without_actor() -> None:
    role = ClubRoleFactory.create()
    account = role.account
    account_pk = account.pk

    with acting_as(account):
        account.delete()
    connection.check_constraints()

    [event] = AuditEvent.objects.filter(action=AuditAction.CLUB_ROLE_REMOVE)
    assert event.actor_user is None
    assert event.before is not None
    assert event.before["account"] == account_pk


def test_a_queryset_delete_of_the_actor_records_its_grants_without_actor() -> None:
    account = UserFactory.create()
    application = PartnerApplicationFactory.create(user=account)
    OrganizerApplicationGrantFactory.create(application=application)

    with acting_as(account):
        get_user_model().objects.filter(pk=account.pk).delete()
    connection.check_constraints()

    [event] = AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_APPLICATION_GRANT_REMOVE
    )
    assert event.actor_user is None


def test_a_staff_account_deleting_itself_in_the_admin_succeeds(
    client: Client,
) -> None:
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    ClubRoleFactory.create(account=account)
    client.force_login(account)

    response = client.post(
        reverse("admin:accounts_user_delete", args=[account.pk]), {"post": "yes"}
    )
    connection.check_constraints()

    assert response.status_code == 302
    assert not get_user_model().objects.filter(pk=account.pk).exists()


def test_another_actor_deleting_an_account_is_still_recorded() -> None:
    role = ClubRoleFactory.create()
    actor = UserFactory.create()

    with acting_as(actor):
        role.account.delete()

    [event] = AuditEvent.objects.filter(action=AuditAction.CLUB_ROLE_REMOVE)
    assert event.actor_user == actor

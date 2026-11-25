"""Admin deletes record the access rows they cascade to (#158, ADR 0033).

Deleting a Club, an Organizer, or an Account through the admin (the delete
view or ``delete_selected``) records one removal for each Organizer Member,
Club Role, and grant the delete cascades to, before the delete, with the
admin as actor. A refused delete records nothing. Outside the admin,
nothing records: a plain ORM save or delete writes no Audit Event. Made-up
data only.
"""

from typing import TYPE_CHECKING

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import Club, ClubRoleKind, MemberRole, Organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verify,
)
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    PartnerApplicationFactory,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

ACCESS_PREFIXES = (
    "organizer_member.",
    "club_role.",
    "club_organizer_grant.",
    "organizer_application_grant.",
)


@pytest.fixture
def superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


@pytest.fixture
def admin_client(client: Client, superuser: "_User") -> Client:
    client.force_login(superuser)
    return client


def _access_events() -> list[AuditEvent]:
    return [
        event
        for event in AuditEvent.objects.order_by("id")
        if event.action.startswith(ACCESS_PREFIXES)
    ]


def _removed() -> set[tuple[str, str]]:
    return {(e.action, e.target_object_id) for e in _access_events()}


def _delete_selected(client: Client, changelist: str, pks: list[int]) -> int:
    response = client.post(
        reverse(changelist),
        {
            "action": "delete_selected",
            "_selected_action": [str(pk) for pk in pks],
            "post": "yes",
        },
    )
    status: int = response.status_code
    return status


# Outside the admin, nothing records.


def test_a_plain_orm_create_save_and_delete_record_nothing() -> None:
    member = OrganizerMemberFactory.create(role=MemberRole.MATCH_MANAGER)
    role = ClubRoleFactory.create()
    grant = ClubOrganizerGrantFactory.create()
    application_grant = OrganizerApplicationGrantFactory.create()

    member.role = MemberRole.ADMIN
    member.save()
    for row in (member, role, grant, application_grant):
        row.delete()

    assert _access_events() == []


def test_a_cascade_outside_the_admin_records_nothing() -> None:
    club = ClubFactory.create()
    ClubRoleFactory.create(club=club)
    ClubOrganizerGrantFactory.create(club=club)

    club.delete()

    assert _access_events() == []


# A Club deleted in the admin.


def test_deleting_a_club_in_the_admin_records_its_roles_and_grants(
    admin_client: Client, superuser: "_User"
) -> None:
    club = ClubFactory.create()
    role = ClubRoleFactory.create(club=club)
    grant = ClubOrganizerGrantFactory.create(club=club)

    response = admin_client.post(
        reverse("admin:clubs_club_delete", args=[club.pk]), {"post": "yes"}
    )

    assert response.status_code == 302
    assert not Club.objects.filter(pk=club.pk).exists()
    assert _removed() == {
        (AuditAction.CLUB_ROLE_REMOVE, str(role.pk)),
        (AuditAction.CLUB_ORGANIZER_GRANT_REMOVE, str(grant.pk)),
    }
    events = _access_events()
    assert {e.actor_user for e in events} == {superuser}
    [role_event] = [e for e in events if e.action == AuditAction.CLUB_ROLE_REMOVE]
    assert role_event.before == {
        "club": club.public_code,
        "account": role.account_id,
        "role": ClubRoleKind.ADMIN,
    }
    assert role_event.after is None


def test_delete_selected_clubs_records_each_cascaded_row(
    admin_client: Client, superuser: "_User"
) -> None:
    clubs = ClubFactory.create_batch(2)
    roles = [ClubRoleFactory.create(club=club) for club in clubs]

    status = _delete_selected(
        admin_client, "admin:clubs_club_changelist", [c.pk for c in clubs]
    )

    assert status == 302
    assert _removed() == {(AuditAction.CLUB_ROLE_REMOVE, str(r.pk)) for r in roles}
    assert {e.actor_user for e in _access_events()} == {superuser}


# An Organizer deleted in the admin.


def test_deleting_an_organizer_in_the_admin_records_members_and_grants(
    admin_client: Client, superuser: "_User"
) -> None:
    organizer = OrganizerFactory.create()
    member = OrganizerMemberFactory.create(organizer=organizer)
    club_grant = ClubOrganizerGrantFactory.create(organizer=organizer)
    application_grant = OrganizerApplicationGrantFactory.create(organizer=organizer)

    response = admin_client.post(
        reverse("admin:clubs_organizer_delete", args=[organizer.pk]),
        {"post": "yes"},
    )

    assert response.status_code == 302
    assert not Organizer.objects.filter(pk=organizer.pk).exists()
    assert _removed() == {
        (AuditAction.ORGANIZER_MEMBER_REMOVE, str(member.pk)),
        (AuditAction.CLUB_ORGANIZER_GRANT_REMOVE, str(club_grant.pk)),
        (AuditAction.ORGANIZER_APPLICATION_GRANT_REMOVE, str(application_grant.pk)),
    }
    assert {e.actor_user for e in _access_events()} == {superuser}
    stored = str([[e.before, e.after] for e in _access_events()])
    assert organizer.name not in stored
    assert member.account.email not in stored


def test_delete_selected_organizers_records_each_member(
    admin_client: Client, superuser: "_User"
) -> None:
    members = OrganizerMemberFactory.create_batch(2)

    status = _delete_selected(
        admin_client,
        "admin:clubs_organizer_changelist",
        [m.organizer_id for m in members],
    )

    assert status == 302
    assert _removed() == {
        (AuditAction.ORGANIZER_MEMBER_REMOVE, str(m.pk)) for m in members
    }
    assert {e.actor_user for e in _access_events()} == {superuser}


def test_a_refused_verified_organizer_delete_selected_records_nothing(
    admin_client: Client,
) -> None:
    organizer = verify(OrganizerFactory.create())
    OrganizerMemberFactory.create(organizer=organizer)
    before = AuditEvent.objects.count()

    _delete_selected(admin_client, "admin:clubs_organizer_changelist", [organizer.pk])

    assert Organizer.objects.filter(pk=organizer.pk).exists()
    assert AuditEvent.objects.count() == before


# An Account deleted in the admin.


def test_deleting_an_account_in_the_admin_records_its_roles_and_grants(
    admin_client: Client, superuser: "_User"
) -> None:
    role = ClubRoleFactory.create()
    account = role.account
    application = PartnerApplicationFactory.create(user=account)
    grant = OrganizerApplicationGrantFactory.create(application=application)

    response = admin_client.post(
        reverse("admin:accounts_user_delete", args=[account.pk]), {"post": "yes"}
    )

    assert response.status_code == 302
    assert _removed() == {
        (AuditAction.CLUB_ROLE_REMOVE, str(role.pk)),
        (AuditAction.ORGANIZER_APPLICATION_GRANT_REMOVE, str(grant.pk)),
    }
    assert {e.actor_user for e in _access_events()} == {superuser}
    assert account.email not in str([e.before for e in _access_events()])


def test_delete_selected_accounts_records_each_role(
    admin_client: Client, superuser: "_User"
) -> None:
    roles = ClubRoleFactory.create_batch(2)

    status = _delete_selected(
        admin_client, "admin:accounts_user_changelist", [r.account_id for r in roles]
    )

    assert status == 302
    assert _removed() == {(AuditAction.CLUB_ROLE_REMOVE, str(r.pk)) for r in roles}
    assert {e.actor_user for e in _access_events()} == {superuser}


def test_a_staff_account_deleting_itself_records_its_role_without_actor(
    client: Client,
) -> None:
    # An event naming the deleted Account would fail the delete at commit.
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    role = ClubRoleFactory.create(account=account)
    client.force_login(account)

    response = client.post(
        reverse("admin:accounts_user_delete", args=[account.pk]), {"post": "yes"}
    )
    connection.check_constraints()

    assert response.status_code == 302
    assert not get_user_model().objects.filter(pk=account.pk).exists()
    [event] = _access_events()
    assert (event.action, event.target_object_id) == (
        AuditAction.CLUB_ROLE_REMOVE,
        str(role.pk),
    )
    assert event.actor_user is None
    assert event.before is not None
    assert event.before["account"] == account.pk


def test_a_staff_account_in_its_own_delete_selected_records_without_actor(
    client: Client,
) -> None:
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    ClubRoleFactory.create(account=account)
    other_role = ClubRoleFactory.create()
    client.force_login(account)

    status = _delete_selected(
        client,
        "admin:accounts_user_changelist",
        [account.pk, other_role.account_id],
    )
    connection.check_constraints()

    assert status == 302
    assert {e.actor_user for e in _access_events()} == {None}
    assert len(_access_events()) == 2


# A Club Role moved to another Account through the inline.


def test_moving_a_club_role_to_another_account_in_the_inline_records_one_change(
    admin_client: Client, superuser: "_User"
) -> None:
    role = ClubRoleFactory.create()
    club = role.club
    other = UserFactory.create()
    old_account = role.account_id

    response = admin_client.post(
        reverse("admin:clubs_club_change", args=[club.pk]),
        {
            "name": club.name,
            "city": club.city,
            "region": club.region,
            "roles-TOTAL_FORMS": "1",
            "roles-INITIAL_FORMS": "1",
            "roles-MIN_NUM_FORMS": "0",
            "roles-MAX_NUM_FORMS": "1000",
            "roles-0-id": str(role.pk),
            "roles-0-club": str(club.pk),
            "roles-0-account": str(other.pk),
            "roles-0-role": ClubRoleKind.ADMIN,
        },
    )

    assert response.status_code == 302
    [event] = _access_events()
    assert event.action == AuditAction.CLUB_ROLE_CHANGE
    assert event.actor_user == superuser
    assert event.before == {
        "club": club.public_code,
        "account": old_account,
        "role": ClubRoleKind.ADMIN,
    }
    assert event.after == {**event.before, "account": other.pk}

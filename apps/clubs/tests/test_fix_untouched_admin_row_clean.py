"""The admin role rule applies only to a new or changed row (#232 review).

``OrganizerMember.clean()`` and ``ClubRole.clean()`` refuse an admin role
for an inactive or unverified Account only when the row is new or its
account or role changed. An untouched admin row whose Account staff
deactivated must not block saving its Organizer or Club in the admin,
which validates every inline form. Made-up data only.
"""

from typing import TYPE_CHECKING

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import ClubRole, MemberRole, Organizer, OrganizerMember
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verify_login_address,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


def _deactivated_admin(organizer: Organizer) -> OrganizerMember:
    member: OrganizerMember = OrganizerMemberFactory.create(
        organizer=organizer,
        account=verify_login_address(UserFactory.create()),
        role=MemberRole.ADMIN,
    )
    member.account.is_active = False
    member.account.save()
    return member


def test_an_untouched_admin_row_with_an_inactive_account_is_valid() -> None:
    member = _deactivated_admin(OrganizerFactory.create())

    OrganizerMember.objects.get(pk=member.pk).full_clean()


def test_an_untouched_club_admin_row_with_an_inactive_account_is_valid() -> None:
    role = ClubRoleFactory.create(account=verify_login_address(UserFactory.create()))
    role.account.is_active = False
    role.account.save()

    ClubRole.objects.get(pk=role.pk).full_clean()


def test_promoting_a_stored_row_of_an_inactive_account_is_still_refused() -> None:
    member: OrganizerMember = OrganizerMemberFactory.create(
        account=verify_login_address(UserFactory.create())
    )
    member.account.is_active = False
    member.account.save()
    stored = OrganizerMember.objects.get(pk=member.pk)
    stored.role = MemberRole.ADMIN

    with pytest.raises(ValidationError):
        stored.full_clean()


def test_moving_a_stored_row_to_an_unverified_account_is_still_refused() -> None:
    role = ClubRoleFactory.create(account=verify_login_address(UserFactory.create()))
    stored = ClubRole.objects.get(pk=role.pk)
    stored.account = UserFactory.create()

    with pytest.raises(ValidationError):
        stored.full_clean()


def test_renaming_an_organizer_with_a_deactivated_admin_saves(client: Client) -> None:
    organizer = OrganizerFactory.create()
    member = _deactivated_admin(organizer)
    staff: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(staff)

    response = client.post(
        reverse("admin:clubs_organizer_change", args=[organizer.pk]),
        {
            "name": "Made-up Renamed Ladder",
            "members-TOTAL_FORMS": "1",
            "members-INITIAL_FORMS": "1",
            "members-MIN_NUM_FORMS": "0",
            "members-MAX_NUM_FORMS": "1000",
            "members-0-id": str(member.pk),
            "members-0-organizer": str(organizer.pk),
            "members-0-account": str(member.account_id),
            "members-0-role": MemberRole.ADMIN,
        },
    )

    assert response.status_code == 302
    organizer.refresh_from_db()
    assert organizer.name == "Made-up Renamed Ladder"


def test_renaming_a_club_with_a_deactivated_admin_saves(client: Client) -> None:
    club = ClubFactory.create()
    role = ClubRoleFactory.create(
        club=club, account=verify_login_address(UserFactory.create())
    )
    role.account.is_active = False
    role.account.save()
    staff: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(staff)

    response = client.post(
        reverse("admin:clubs_club_change", args=[club.pk]),
        {
            "name": "Made-up Renamed Club",
            "city": club.city,
            "region": club.region,
            "roles-TOTAL_FORMS": "1",
            "roles-INITIAL_FORMS": "1",
            "roles-MIN_NUM_FORMS": "0",
            "roles-MAX_NUM_FORMS": "1000",
            "roles-0-id": str(role.pk),
            "roles-0-club": str(club.pk),
            "roles-0-account": str(role.account_id),
            "roles-0-role": "admin",
        },
    )

    assert response.status_code == 302
    club.refresh_from_db()
    assert club.name == "Made-up Renamed Club"

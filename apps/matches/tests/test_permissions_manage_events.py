"""Who may manage an Organizer's Events (#125, ADR 0021).

An active member (either role) of the Organizer, while it is verified.
Made-up data only.
"""

import pytest
from django.contrib.auth.models import AnonymousUser, Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import ClubRoleKind, MemberRole, Organizer
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.matches.permissions import can_manage_events
from apps.partners.tests.factories import approved_application

pytestmark = pytest.mark.django_db


def _member(organizer: Organizer, role: MemberRole = MemberRole.ADMIN) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=account, role=role)
    return account


@pytest.mark.parametrize("role", list(MemberRole))
def test_member_of_verified_organizer_may_manage(role: MemberRole) -> None:
    organizer = verified_organizer()
    assert can_manage_events(_member(organizer, role), organizer.pk)


def test_member_of_unverified_organizer_may_not() -> None:
    organizer = OrganizerFactory.create()
    assert not can_manage_events(_member(organizer), organizer.pk)


def test_member_after_unverify_may_not() -> None:
    organizer = verified_organizer()
    account = _member(organizer)
    staff: User = UserFactory.create()
    staff.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    unverify_organizer(organizer, actor=staff)
    assert not can_manage_events(account, organizer.pk)


def test_non_member_may_not() -> None:
    organizer = verified_organizer()
    assert not can_manage_events(UserFactory.create(), organizer.pk)


def test_member_of_another_organizer_may_not() -> None:
    organizer = verified_organizer()
    other = verified_organizer()
    assert not can_manage_events(_member(other), organizer.pk)


def test_inactive_member_may_not() -> None:
    organizer = verified_organizer()
    account = _member(organizer)
    User.objects.filter(pk=account.pk).update(is_active=False)
    # The instance still says active: the rule reads the database.
    assert account.is_active
    assert not can_manage_events(account, organizer.pk)


def test_club_admin_may_not() -> None:
    organizer = verified_organizer()
    account: User = UserFactory.create()
    ClubRoleFactory.create(
        club=ClubFactory.create(), account=account, role=ClubRoleKind.ADMIN
    )
    assert not can_manage_events(account, organizer.pk)


def test_anonymous_and_unsaved_and_application_may_not() -> None:
    organizer = verified_organizer()
    assert not can_manage_events(AnonymousUser(), organizer.pk)
    assert not can_manage_events(User(email="unsaved@example.test"), organizer.pk)
    application = approved_application(organizer=organizer)
    assert not can_manage_events(application, organizer.pk)


def test_unknown_organizer_id_may_not() -> None:
    organizer = verified_organizer()
    account = _member(organizer)
    assert not can_manage_events(account, organizer.pk + 100000)

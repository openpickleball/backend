"""A grant row moved to another parent tells both sides (#232 review).

When staff move a Club Organizer Grant to another Club or Organizer, or
an Organizer Application Grant to another Organizer, in the admin, the
old side's admins get a removed notice and the new side's an added
notice. Made-up data only.
"""

from collections.abc import Callable
from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.access import save_access_row
from apps.clubs.emails import SECURITY_NOTICE
from apps.clubs.models import Club, ClubOrganizerGrant, MemberRole, Organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
)
from apps.emails.models import QueuedEmail
from apps.partners.models import OrganizerApplicationGrant
from apps.partners.tests.factories import OrganizerApplicationGrantFactory

pytestmark = pytest.mark.django_db


def _club_admin(club: Club) -> User:
    account: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=account)
    return account


def _organizer_admin(organizer: Organizer) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    return account


def _bodies(capture: Any, change: Callable[[], object]) -> dict[str, list[str]]:
    with capture(execute=False) as callbacks:
        change()
    for callback in callbacks:
        callback()
    bodies: dict[str, list[str]] = {}
    for row in QueuedEmail.objects.filter(template=SECURITY_NOTICE).order_by("pk"):
        bodies.setdefault(row.to, []).append(row.body)
    return bodies


def _staff() -> User:
    staff: User = UserFactory.create(is_staff=True, is_superuser=True)
    return staff


def test_moving_a_club_grant_to_another_club_tells_both_clubs(
    django_capture_on_commit_callbacks: Any,
) -> None:
    grant: ClubOrganizerGrant = ClubOrganizerGrantFactory.create()
    old_admin = _club_admin(grant.club)
    new_club = ClubFactory.create()
    new_admin = _club_admin(new_club)
    grant.club = new_club

    bodies = _bodies(
        django_capture_on_commit_callbacks,
        lambda: save_access_row(grant, actor=_staff()),
    )

    [old] = bodies[old_admin.email]
    [new] = bodies[new_admin.email]
    assert "no longer allows" in old
    assert "now allows" in new


def test_moving_a_club_grant_to_another_organizer_tells_the_club_both(
    django_capture_on_commit_callbacks: Any,
) -> None:
    grant: ClubOrganizerGrant = ClubOrganizerGrantFactory.create()
    admin = _club_admin(grant.club)
    old_code = grant.organizer.public_code
    grant.organizer = OrganizerFactory.create()

    bodies = _bodies(
        django_capture_on_commit_callbacks,
        lambda: save_access_row(grant, actor=_staff()),
    )

    removed, added = bodies[admin.email]
    assert "no longer allows" in removed
    assert old_code in removed
    assert grant.organizer.public_code in added


def test_moving_an_application_grant_tells_both_organizers(
    django_capture_on_commit_callbacks: Any,
) -> None:
    grant: OrganizerApplicationGrant = OrganizerApplicationGrantFactory.create()
    old_admin = _organizer_admin(grant.organizer)
    new_organizer = OrganizerFactory.create()
    new_admin = _organizer_admin(new_organizer)
    grant.organizer = new_organizer

    bodies = _bodies(
        django_capture_on_commit_callbacks,
        lambda: save_access_row(grant, actor=_staff()),
    )

    [old] = bodies[old_admin.email]
    [new] = bodies[new_admin.email]
    assert "no longer has permission" in old
    assert "was given permission" in new

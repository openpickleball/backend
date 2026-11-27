"""An admin row moved to another Account tells the old Account it was
removed (#232 review).

When staff move an admin row from Account A to Account B in the admin,
A and the other admins get a removed notice for A, and the other admins
an added notice for B. Made-up data only.
"""

from collections.abc import Callable
from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.access import save_access_row
from apps.clubs.emails import SECURITY_NOTICE
from apps.clubs.models import ClubRole, MemberRole, OrganizerMember
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verify_login_address,
)
from apps.emails.models import QueuedEmail

pytestmark = pytest.mark.django_db


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


def test_moving_an_organizer_admin_row_tells_the_old_account_it_was_removed(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    other: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=organizer, account=other, role=MemberRole.ADMIN
    )
    old: User = UserFactory.create()
    member: OrganizerMember = OrganizerMemberFactory.create(
        organizer=organizer, account=old, role=MemberRole.ADMIN
    )
    new = verify_login_address(UserFactory.create())
    member.account = new

    bodies = _bodies(
        django_capture_on_commit_callbacks,
        lambda: save_access_row(member, actor=_staff()),
    )

    [told] = bodies[old.email]
    assert "was removed from organizer" in told
    removed, added = bodies[other.email]
    assert "was removed from organizer" in removed
    assert "was added to organizer" in added
    assert new.email not in bodies


def test_moving_a_club_admin_row_tells_the_old_account_it_was_removed(
    django_capture_on_commit_callbacks: Any,
) -> None:
    club = ClubFactory.create()
    other: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=other)
    old: User = UserFactory.create()
    role: ClubRole = ClubRoleFactory.create(club=club, account=old)
    new = verify_login_address(UserFactory.create())
    role.account = new

    bodies = _bodies(
        django_capture_on_commit_callbacks,
        lambda: save_access_row(role, actor=_staff()),
    )

    [told] = bodies[old.email]
    assert "An admin was removed from club" in told
    removed, added = bodies[other.email]
    assert "An admin was removed from club" in removed
    assert "An admin was added to club" in added
    assert new.email not in bodies

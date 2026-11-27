"""A demoted or removed admin hears of it (#232 review).

An attacker on one admin's Account can demote or remove the only other
admin, and the other admins' notice then reaches nobody. So on a removal,
and on a role change away from admin, the Account the change is about
gets the notice too, when it is active and is not the actor. Made-up
data only.
"""

from collections.abc import Callable
from typing import Any

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.deletion import delete_account
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.clubs import club_self_service, self_service
from apps.clubs.emails import SECURITY_NOTICE
from apps.clubs.models import Club, MemberRole, Organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
)
from apps.emails.models import QueuedEmail
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _account() -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    enroll_totp(account)
    PlayerFactory.create(account=account)
    return account


def _admins(organizer: Organizer) -> tuple[User, User]:
    first, second = _account(), _account()
    for account in (first, second):
        OrganizerMemberFactory.create(
            organizer=organizer, account=account, role=MemberRole.ADMIN
        )
    return first, second


def _club_admins(club: Club) -> tuple[User, User]:
    first, second = _account(), _account()
    for account in (first, second):
        ClubRoleFactory.create(club=club, account=account)
    return first, second


def _recipients(capture: Any, change: Callable[[], object]) -> set[str]:
    with capture(execute=False) as callbacks:
        change()
    for callback in callbacks:
        callback()
    return set(
        QueuedEmail.objects.filter(template=SECURITY_NOTICE).values_list(
            "to", flat=True
        )
    )


def test_a_demoted_admin_is_told(django_capture_on_commit_callbacks: Any) -> None:
    organizer = OrganizerFactory.create()
    actor, demoted = _admins(organizer)

    recipients = _recipients(
        django_capture_on_commit_callbacks,
        lambda: self_service.change_member_role(
            organizer.pk,
            demoted.player.public_code,
            actor=actor,
            role=MemberRole.MATCH_MANAGER,
        ),
    )

    assert recipients == {demoted.email}


def test_a_removed_admin_is_told(django_capture_on_commit_callbacks: Any) -> None:
    organizer = OrganizerFactory.create()
    actor, removed = _admins(organizer)

    recipients = _recipients(
        django_capture_on_commit_callbacks,
        lambda: self_service.remove_member(
            organizer.pk, removed.player.public_code, actor=actor
        ),
    )

    assert recipients == {removed.email}


def test_an_admin_who_leaves_is_not_told(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    leaving, other = _admins(organizer)

    recipients = _recipients(
        django_capture_on_commit_callbacks,
        lambda: self_service.remove_member(
            organizer.pk, leaving.player.public_code, actor=leaving
        ),
    )

    assert recipients == {other.email}


def test_a_removed_club_admin_is_told(
    django_capture_on_commit_callbacks: Any,
) -> None:
    club = ClubFactory.create()
    actor, removed = _club_admins(club)

    recipients = _recipients(
        django_capture_on_commit_callbacks,
        lambda: club_self_service.remove_admin(
            club.pk, removed.player.public_code, actor=actor
        ),
    )

    assert recipients == {removed.email}


def test_a_deleted_account_is_not_told(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    other, leaving = _admins(organizer)

    recipients = _recipients(
        django_capture_on_commit_callbacks,
        lambda: delete_account(leaving, actor=leaving),
    )

    assert recipients == {other.email}

"""Account deletion and Club Invites (#230, ADR 0048).

Deletion removes the Club Invites addressed to the Account's addresses
and the ones it sent, also when the Account row is kept. Made-up data
only.
"""

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.deletion import delete_account
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.clubs.models import ClubInvite
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubInviteFactory,
    ClubRoleFactory,
)

pytestmark = pytest.mark.django_db


def test_club_invites_to_the_accounts_addresses_are_deleted() -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(user=account, email="second.made-up@example.com")
    ClubInviteFactory.create(email=account.email)
    ClubInviteFactory.create(email="second.made-up@example.com")
    other = ClubInviteFactory.create(email="someone.made-up@example.com")

    delete_account(account, actor=account)

    assert list(ClubInvite.objects.values_list("pk", flat=True)) == [other.pk]


def test_club_invites_the_account_sent_are_deleted_even_when_it_is_kept() -> None:
    account = UserFactory.create()
    club = ClubFactory.create()
    ClubRoleFactory.create(club=club, account=account)
    ClubRoleFactory.create(club=club)
    ClubInviteFactory.create(club=club, invited_by=account)
    kept = ClubInviteFactory.create(club=club)
    # An Account that has acted is kept, so the invites do not cascade.
    record_event(action=AuditAction.DATA_EXPORT_REQUEST, target=account, actor=account)

    result = delete_account(account, actor=account)

    assert result.account_kept is True
    assert list(ClubInvite.objects.values_list("pk", flat=True)) == [kept.pk]

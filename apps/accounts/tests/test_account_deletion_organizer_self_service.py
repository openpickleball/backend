"""Account deletion and Organizer self-service rows (#229, ADR 0048).

Deletion removes the Organizer Invites addressed to the Account's
addresses and the ones it sent, and for an Organizer whose only member
it is, blanks its verification request statements with the name.
Made-up data only.
"""

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.deletion import REMOVED_ORGANIZER_NAME, delete_account
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.clubs.models import MemberRole, OrganizerInvite
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerInviteFactory,
    OrganizerMemberFactory,
    OrganizerVerificationRequestFactory,
)

pytestmark = pytest.mark.django_db


def test_invites_to_the_accounts_addresses_are_deleted() -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(user=account, email="second.made-up@example.com")
    to_login = OrganizerInviteFactory.create(email=account.email)
    to_second = OrganizerInviteFactory.create(email="second.made-up@example.com")
    other = OrganizerInviteFactory.create(email="someone.made-up@example.com")

    delete_account(account, actor=account)

    remaining = set(OrganizerInvite.objects.values_list("pk", flat=True))
    assert remaining == {other.pk}
    assert to_login.pk not in remaining
    assert to_second.pk not in remaining


def test_invites_the_account_sent_are_deleted_even_when_it_is_kept() -> None:
    account = UserFactory.create()
    organizer = OrganizerFactory.create()
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN, mfa=False
    )
    OrganizerMemberFactory.create(organizer=organizer, mfa=False)
    OrganizerInviteFactory.create(organizer=organizer, invited_by=account)
    kept = OrganizerInviteFactory.create(organizer=organizer)
    # An Account that has acted is kept, so the invites do not cascade.
    record_event(action=AuditAction.DATA_EXPORT_REQUEST, target=account, actor=account)

    result = delete_account(account, actor=account)

    assert result.account_kept is True
    assert list(OrganizerInvite.objects.values_list("pk", flat=True)) == [kept.pk]


def test_an_only_members_organizer_loses_its_statements_with_its_name() -> None:
    account = UserFactory.create()
    alone = OrganizerFactory.create(name="Made-up Solo Person")
    shared = OrganizerFactory.create()
    OrganizerMemberFactory.create(organizer=alone, account=account, mfa=False)
    OrganizerMemberFactory.create(organizer=shared, account=account, mfa=False)
    OrganizerMemberFactory.create(organizer=shared, mfa=False)
    solo_request = OrganizerVerificationRequestFactory.create(
        organizer=alone, statement="Made-up solo statement."
    )
    shared_request = OrganizerVerificationRequestFactory.create(
        organizer=shared, statement="Made-up shared statement."
    )

    delete_account(account, actor=account)

    alone.refresh_from_db()
    solo_request.refresh_from_db()
    shared_request.refresh_from_db()
    assert alone.name == REMOVED_ORGANIZER_NAME
    assert solo_request.statement == ""
    assert shared_request.statement == "Made-up shared statement."

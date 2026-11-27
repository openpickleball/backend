"""An invite never outlives its sender's authority (#229, hard rule 8).

An invite is accepted only while its sender is still an active admin
member of that Organizer, and demoting or removing the sender deletes
the sender's open invites to it in the same transaction. Made-up data
only.
"""

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.clubs.models import MemberRole, Organizer, OrganizerInvite, OrganizerMember
from apps.clubs.self_service import (
    NotFound,
    accept_invite,
    change_member_role,
    invite_member,
    remove_member,
)
from apps.clubs.tests.factories import OrganizerFactory, OrganizerMemberFactory
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


def _admin(organizer: Organizer) -> User:
    account = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    return account


def _invite(organizer: Organizer, sender: User, invitee: User) -> str:
    invite_member(organizer.pk, actor=sender, email=invitee.email, role="admin")
    body = QueuedEmail.objects.filter(to=invitee.email).latest("pk").body
    return body.split("/organizer/invites/")[1].split()[0]


def test_an_invite_from_a_removed_admin_cannot_be_accepted() -> None:
    organizer = OrganizerFactory.create()
    sender, other = _admin(organizer), _admin(organizer)
    invitee = _account()
    key = _invite(organizer, sender, invitee)

    remove_member(organizer.pk, sender.player.public_code, actor=other)

    with pytest.raises(NotFound):
        accept_invite(key, actor=invitee)
    assert not OrganizerMember.objects.filter(account=invitee).exists()


def test_demoting_or_removing_the_sender_deletes_only_their_invites() -> None:
    organizer = OrganizerFactory.create()
    sender, other = _admin(organizer), _admin(organizer)
    _invite(organizer, sender, _account())
    kept_invitee = _account()
    _invite(organizer, other, kept_invitee)

    change_member_role(
        organizer.pk, sender.player.public_code, actor=other, role="match_manager"
    )

    assert list(OrganizerInvite.objects.values_list("email", flat=True)) == [
        kept_invitee.email
    ]


@pytest.mark.parametrize("change", ["demoted", "inactive", "gone"])
def test_accept_refuses_when_the_sender_is_no_longer_an_active_admin(
    change: str,
) -> None:
    organizer = OrganizerFactory.create()
    sender = _admin(organizer)
    _admin(organizer)
    invitee = _account()
    key = _invite(organizer, sender, invitee)
    # Raw writes, which skip the services' invite cleanup.
    members = OrganizerMember.objects.filter(organizer=organizer, account=sender)
    if change == "demoted":
        members.update(role=MemberRole.MATCH_MANAGER)
    elif change == "inactive":
        User.objects.filter(pk=sender.pk).update(is_active=False)
    else:
        members.delete()

    with pytest.raises(NotFound):
        accept_invite(key, actor=invitee)
    assert not OrganizerMember.objects.filter(account=invitee).exists()

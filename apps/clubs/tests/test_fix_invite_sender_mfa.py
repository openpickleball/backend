"""An invite from an admin who lost MFA cannot be accepted (#229).

Admin authority needs MFA (ADR 0043), so an admin who removed their
TOTP authenticator no longer acts for the Organizer, and the invites
they sent give no more authority than they now hold. Made-up data only.
"""

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.clubs.models import MemberRole, OrganizerMember
from apps.clubs.self_service import NotFound, accept_invite, invite_member
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


def test_an_invite_from_an_admin_without_mfa_cannot_be_accepted() -> None:
    organizer = OrganizerFactory.create()
    sender = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=sender, role=MemberRole.ADMIN
    )
    invitee = _account()
    invite_member(organizer.pk, actor=sender, email=invitee.email, role="admin")
    body = QueuedEmail.objects.get(to=invitee.email).body
    key = body.split("/organizer/invites/")[1].split()[0]

    Authenticator.objects.filter(
        user_id=sender.pk, type=Authenticator.Type.TOTP
    ).delete()

    with pytest.raises(NotFound):
        accept_invite(key, actor=invitee)
    assert not OrganizerMember.objects.filter(account=invitee).exists()

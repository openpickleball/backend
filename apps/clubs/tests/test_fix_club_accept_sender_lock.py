"""A Club invite is not accepted while its sender is losing authority (#230).

Account deletion and TOTP removal lock the sender's Account
``FOR UPDATE`` and never lock the Club, so the Club lock alone does not
make an accept wait for them. While another connection holds the
sender's Account that way and then deactivates it, removes its Club
Role, or removes its TOTP authenticator, an accept must wait for that
transaction and then refuse. Runs in real transactions, with Audit
Events switched off (the audit table refuses the teardown's flush once
it has a row). Made-up data only.
"""

import threading
import time
from collections.abc import Callable

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.db import connection, transaction

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit import services as audit_services
from apps.clubs.club_self_service import accept_club_invite
from apps.clubs.invites import hash_invite_key, new_invite_key
from apps.clubs.models import Club, ClubRole
from apps.clubs.self_service import NotFound
from apps.clubs.tests.factories import ClubFactory, ClubInviteFactory, ClubRoleFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db(transaction=True)

# How long the other connection holds the sender's Account before it
# changes it. An accept that does not wait finishes well inside it.
HOLD_SECONDS = 1.5


def _account() -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    enroll_totp(account)
    PlayerFactory.create(account=account)
    return account


def _deactivate(sender: User, club: Club) -> None:
    User.objects.filter(pk=sender.pk).update(is_active=False)


def _remove_role(sender: User, club: Club) -> None:
    ClubRole.objects.filter(club=club, account_id=sender.pk).delete()


def _remove_totp(sender: User, club: Club) -> None:
    Authenticator.objects.filter(user_id=sender.pk).delete()


@pytest.mark.parametrize("change", [_deactivate, _remove_role, _remove_totp])
def test_an_accept_waits_for_the_sender_lock_then_refuses(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[User, Club], None]
) -> None:
    monkeypatch.setattr(audit_services, "record_event", lambda **kwargs: None)
    club = ClubFactory.create()
    sender = _account()
    ClubRoleFactory.create(club=club, account=sender)
    ClubRoleFactory.create(club=club, account=_account())
    invitee = _account()
    key = new_invite_key()
    ClubInviteFactory.create(
        club=club,
        email=invitee.email,
        invited_by=sender,
        key_hash=hash_invite_key(key),
    )
    locked = threading.Event()

    def hold_sender() -> None:
        try:
            with transaction.atomic():
                list(User.objects.select_for_update().filter(pk=sender.pk))
                locked.set()
                time.sleep(HOLD_SECONDS)
                change(sender, club)
        finally:
            connection.close()

    other = threading.Thread(target=hold_sender)
    other.start()
    assert locked.wait(timeout=10)
    try:
        with pytest.raises(NotFound):
            accept_club_invite(key, actor=invitee)
    finally:
        other.join(timeout=30)

    assert not ClubRole.objects.filter(club=club, account=invitee).exists()

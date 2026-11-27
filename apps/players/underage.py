"""A moderator's removal of a player under 13 (#190, ADR 0059).

OPEN refuses anyone under 13 at sign-up, but a player can give a wrong
birth year and month. When a moderator learns a player is under 13,
``remove_underage_player`` removes the personal data of the Player's
Account with Account deletion's own steps (``delete_account``, ADR 0048),
so the Player is shown exactly as after Account deletion: its OPEN Code,
Matches, and Ratings stay in the Ledger, and the Account cannot sign in
again. It records one ``player.underage_remove`` Audit Event with the
moderator as actor, and no free-text reason, so the event holds no
personal data.
"""

from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.accounts.deletion import (
    AccountAlreadyDeleted,
    AccountDeletion,
    delete_account,
    is_deleted_email,
)
from apps.accounts.locks import hold_active_account
from apps.audit.models import AuditAction
from apps.players.models import Player

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser, _User

REMOVE_UNDERAGE_PERMISSION = "players.remove_underage_player"


class UnderageRemovalRefused(PermissionError):
    """The moderator may not remove this Player's personal data.

    The message is fixed and names nobody.
    """

    def __init__(self) -> None:
        super().__init__("This removal is not allowed.")


def remove_underage_player(
    player: Player, *, moderator: "_User | AnonymousUser"
) -> AccountDeletion:
    """Remove the personal data of ``player``'s Account, as Account deletion does.

    Only an active Account with ``players.remove_underage_player`` (an
    active superuser holds it) may run it, checked on the stored Account
    under its lock and on a freshly loaded instance, so a stale in-memory
    Account or permission cache counts for nothing. It refuses
    (``UnderageRemovalRefused``) the moderator's own Player, and a Player
    whose Account is staff, superuser, or holds a group or permission (as
    the Account admin refuses, #317). A signed-out moderator is refused. It raises ``AccountAlreadyDeleted``
    when a deletion already cleared the Account. Every refusal comes
    before any write, so it changes nothing.

    Lock order: the moderator's Account ``FOR KEY SHARE``
    (``hold_active_account``), then the Player's Account ``FOR UPDATE``,
    then what ``delete_account`` takes (it locks the same Account again,
    then the Player and the Organizers). Runs in one transaction.
    """
    if moderator.pk is None or moderator.pk == player.account_id:
        raise UnderageRemovalRefused
    user_model = get_user_model()
    with transaction.atomic():
        if not hold_active_account(moderator.pk):
            raise UnderageRemovalRefused
        fresh = user_model.objects.get(pk=moderator.pk)
        if not fresh.has_perm(REMOVE_UNDERAGE_PERMISSION):
            raise UnderageRemovalRefused
        target = (
            user_model.objects.select_for_update().filter(pk=player.account_id).first()
        )
        if target is None or (not target.is_active and is_deleted_email(target.email)):
            raise AccountAlreadyDeleted
        if (
            target.is_staff
            or target.is_superuser
            or target.groups.exists()
            or target.user_permissions.exists()
        ):
            raise UnderageRemovalRefused
        return delete_account(
            target, actor=fresh, action=AuditAction.PLAYER_UNDERAGE_REMOVE
        )

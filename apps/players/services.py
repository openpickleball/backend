"""Create Players and change their profile fields and privacy settings.

Only the player changes their own Player (ADR 0009). ``update_profile`` and
``update_privacy`` refuse any other actor before writing anything. Each
change records one Audit Event in the same transaction. A profile change
records only the changed field names, never a value, because these are
identifying personal data fields (ADR 0009). A privacy change records the
changed settings' levels.
"""

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import transaction

from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot
from apps.players.age import is_minor
from apps.players.models import Player, PlayerPrivacy, Visibility, normalize_input

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

PROFILE_FIELDS: tuple[str, ...] = (
    "display_name",
    "legal_name",
    "birth_year",
    "birth_month",
    "gender_division",
    "city",
    "region",
)


class NotOwnPlayer(PermissionError):
    """The actor is not the Player's own active Account."""


def create_player(
    *,
    account: "_User",
    display_name: str,
    birth_year: int,
    birth_month: int,
    gender_division: str,
    legal_name: str | None = "",
    city: str | None = "",
    region: str | None = "",
) -> Player:
    """Create a Player for ``account``, with its privacy settings.

    Raises ``ValidationError`` for an invalid or missing value, for a
    player under 13 on the age limit date (``apps.players.age``), or when
    the Account already has a Player. ``gender_division`` is required,
    ``mens`` or ``womens`` (issue #215). None for an optional text field
    means empty. A player aged 13 to 17 starts with every privacy setting
    ``PRIVATE``; an older one gets the model defaults. Turning 18 later
    changes no setting. Records no Audit Event: creating a Player changes
    no privacy setting.
    """
    with transaction.atomic():
        # Lock the Account row, so two calls for one Account run their
        # one-Player check in turn and the second gets a ValidationError.
        get_user_model().objects.select_for_update().filter(pk=account.pk).first()
        player = Player(
            account=account,
            display_name=display_name,
            legal_name=legal_name or "",
            birth_year=birth_year,
            birth_month=birth_month,
            gender_division=gender_division,
            city=city or "",
            region=region or "",
        )
        player.full_clean()
        player.save()
        if is_minor(player.birth_year, player.birth_month):
            private = dict.fromkeys(PlayerPrivacy.SETTINGS, Visibility.PRIVATE)
            PlayerPrivacy.objects.create(player=player, **private)
        else:
            PlayerPrivacy.objects.create(player=player)
    return player


_NOT_OWN_PLAYER = "Only the player can change their own Player."


def _refuse_unless_owner(player: Player, actor: "_User | AnonymousUser | None") -> None:
    """A first check on the instances passed in, before any query."""
    if (
        actor is None
        or not actor.is_authenticated
        or not actor.is_active
        or actor.pk != player.account_id
    ):
        raise NotOwnPlayer(_NOT_OWN_PLAYER)


def _lock_own_player(player: Player, actor: "_User | AnonymousUser | None") -> Player:
    """Lock and return the stored Player, if ``actor`` is its active Account.

    Call inside the transaction. The check uses the stored rows, so an
    instance changed in memory or loaded before a deactivation cannot pass.
    """
    locked = Player.objects.select_for_update().get(pk=player.pk)
    if actor is None or actor.pk != locked.account_id:
        raise NotOwnPlayer(_NOT_OWN_PLAYER)
    owner_active = (
        get_user_model().objects.filter(pk=locked.account_id, is_active=True).exists()
    )
    if not owner_active:
        raise NotOwnPlayer(_NOT_OWN_PLAYER)
    return locked


def _refuse_unknown(keys: Iterable[object], allowed: tuple[str, ...]) -> None:
    # A fixed message: the keys are client input (CLAUDE.md, API).
    if any(not isinstance(key, str) or key not in allowed for key in keys):
        raise ValueError("Not a field that can be changed.")


def _is_unchanged(name: str, value: object, stored: object) -> bool:
    """True when a submitted profile value matches the stored one.

    It matches as submitted, or once both are normalized, so a stored
    padded value resent as is or stripped is unchanged too.
    """
    if value == stored:
        return True
    return normalize_input(name, value) == normalize_input(name, stored)


def update_profile(
    *,
    player: Player,
    actor: "_User | AnonymousUser | None",
    changes: Mapping[str, object],
) -> Player:
    """Apply ``changes`` to the player's profile fields and return the Player.

    Raises ``NotOwnPlayer`` unless ``actor`` is the Player's own active
    Account, ``ValueError`` for a key outside ``PROFILE_FIELDS``, and
    ``ValidationError`` for an invalid value. Nothing is saved or recorded
    when any of these is raised, or when no value changed.
    """
    _refuse_unless_owner(player, actor)
    _refuse_unknown(changes, PROFILE_FIELDS)
    with transaction.atomic():
        locked = _lock_own_player(player, actor)
        old = {name: getattr(locked, name) for name in PROFILE_FIELDS}
        # A submitted value equal to the stored one (as is, or both
        # normalized) is unchanged: not validated, saved, or listed. So a
        # client that resends the whole profile is not blocked by a stored
        # value a later rule refuses, and an equal value of another type
        # (1990.0) is never written.
        submitted = {
            name: value
            for name, value in changes.items()
            if not _is_unchanged(name, value, old[name])
        }
        for name, value in submitted.items():
            setattr(locked, name, value)
        locked.validate_changes(submitted)
        changed = sorted(
            name for name in submitted if getattr(locked, name) != old[name]
        )
        if changed:
            locked.save(update_fields=changed)
            # Names only: these are identifying fields (ADR 0009).
            record_event(
                action=AuditAction.PLAYER_PROFILE_CHANGE,
                target=locked,
                actor=actor,
                after={"changed_fields": changed},
            )
    player.refresh_from_db()
    return player


def update_privacy(
    *,
    player: Player,
    actor: "_User | AnonymousUser | None",
    changes: Mapping[str, object],
) -> PlayerPrivacy:
    """Apply ``changes`` to the player's privacy settings and return them.

    Raises like ``update_profile``, with keys checked against
    ``PlayerPrivacy.SETTINGS``. The Audit Event targets the Player, so it
    is one of the events about that player (#39).
    """
    _refuse_unless_owner(player, actor)
    _refuse_unknown(changes, PlayerPrivacy.SETTINGS)
    with transaction.atomic():
        locked = _lock_own_player(player, actor)
        privacy = PlayerPrivacy.objects.select_for_update().get(player=locked)
        old = snapshot(privacy, PlayerPrivacy.SETTINGS)
        for name, value in changes.items():
            setattr(privacy, name, value)
        privacy.full_clean()
        new = snapshot(privacy, PlayerPrivacy.SETTINGS)
        changed = sorted(
            name for name in PlayerPrivacy.SETTINGS if new[name] != old[name]
        )
        if changed:
            privacy.save(update_fields=changed)
            record_event(
                action=AuditAction.PRIVACY_CHANGE,
                target=locked,
                actor=actor,
                before={name: old[name] for name in changed},
                after={name: new[name] for name in changed},
            )
    # Replace the caller's cached row, so player.privacy shows the change.
    player.privacy = privacy
    return privacy

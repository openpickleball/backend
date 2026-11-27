"""Club self-service (#230): profiles, admins, and Club Organizer Grants.

An Account sets up its own Club, invites other admins, and decides which
Organizers may name the Club on their Events and matches. None of this
gives submission authority: a Club Role never does, and a Club
Organizer Grant is consent, not trust (ADR 0021). Only a verified
Organizer's members with MFA still submit counted matches.

- ``create_club``: an active Account whose login address is verified
  creates a Club and becomes its only admin. Past
  ``CLUB_CREATE_LIMIT_PER_ACCOUNT`` admin Club Roles it is refused. No
  MFA is needed: a Club with no grants names no one.
- Every other function but ``accept_club_invite`` acts for a Club admin:
  an active Account with MFA (ADR 0043) with an admin Club Role there,
  read from the database. Anyone else gets ``NotFound``, the same as an
  unknown Club, so the answer never says who is an admin.
- ``update_club``, ``invite_admin``, ``list_admins``, ``remove_admin``,
  ``list_grants``, ``grant_organizer``, ``remove_grant``.
- ``accept_club_invite``: the invited Account, signed in with the
  invited login address, verified, and with a Player, becomes an admin,
  while the invite's sender is still an admin there by the same rule
  (active, admin, with MFA). Every refusal is ``NotFound``.

On these paths a Club always keeps one active admin (``Conflict``
otherwise). Account deletion and the staff admin can still leave a Club
without one; staff repair it. Removing an admin deletes the invites it
sent to that Club in the same transaction. Role and grant writes go
through ``apps.audit.access`` with the acting Account. A Club's name,
city, and region are not personal data (a Club is a place or group), so
``club.change`` records their old and new values; no event holds an
address.

Lock order: the acting Account (``FOR KEY SHARE`` through
``hold_active_account``, ``FOR NO KEY UPDATE`` in ``create_club``), then,
in ``accept_club_invite``, the invite sender's Account (``FOR KEY
SHARE``), then the Club row (``FOR UPDATE``), then, in
``grant_organizer``, the Organizer row (``FOR KEY SHARE``), then invite,
role, and grant rows. Account deletion and TOTP removal lock an Account
``FOR UPDATE`` and never the Club, so the Club lock does not make an
accept wait for them: holding the sender does, and the sender's role
and MFA are read after it. Deletion locks the sender before deleting
its invite rows, so the sender comes before the invite row here.
Deletion locks Organizers but never the Club, so the Organizer after the
Club forms no cycle. Never add a Club lock to deletion.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.addresses import has_verified_login_address
from apps.accounts.locks import hold_active_account
from apps.accounts.mfa import accounts_with_mfa
from apps.audit.access import delete_access_row, save_access_row
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.clubs.emails import CLUB_INVITE, render_club_invite
from apps.clubs.invites import (
    find_open_invite,
    hash_invite_key,
    invite_matches_account,
    new_invite_key,
    normalized_invite_email,
)
from apps.clubs.models import (
    Club,
    ClubInvite,
    ClubOrganizerGrant,
    ClubRole,
    ClubRoleKind,
    Organizer,
)
from apps.clubs.self_service import AddressNotVerified, Conflict, NotFound
from apps.emails.services import queue_email
from apps.players.models import Player
from apps.public_ids.codes import CodeType, normalize

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

# Fixed texts, never a submitted value.
ADDRESS_NOT_VERIFIED_MESSAGE: Final = (
    "Verify your email address before creating a club."
)
CREATE_LIMIT_MESSAGE: Final = (
    "This account is already an admin of as many clubs as it may create."
)
OPEN_INVITES_MESSAGE: Final = (
    "This club has too many open invites. Wait until some are accepted or expire."
)
LAST_ADMIN_MESSAGE: Final = "A club must keep at least one admin."
ALREADY_ADMIN_MESSAGE: Final = "You are already an admin of this club."
ALREADY_GRANTED_MESSAGE: Final = "This club already allows that organizer."
# The profile fields an admin may change, in a fixed order.
PROFILE_FIELDS: Final = ("name", "city", "region")


@dataclass(frozen=True)
class ClubAdminView:
    """One Club admin as the Club's admins see it: never an email or a
    row id."""

    player: str | None
    display_name: str | None
    you: bool


@dataclass(frozen=True)
class ClubGrantView:
    """One Organizer the Club allows, by OPEN Code only: an Organizer's
    name may be a person's (ADR 0009, ADR 0050)."""

    organizer: str
    created_at: datetime


def create_club(*, actor: "_User", name: str, city: str = "", region: str = "") -> Club:
    """Create a Club with ``actor`` as its only admin.

    Raises ``AddressNotVerified`` before any write, ``Conflict`` at the
    per-Account limit, ``NotFound`` for an Account deleted or deactivated
    meanwhile, and Django's ``ValidationError`` for a value the model
    refuses. Records ``club.create`` (no values) and ``club_role.add``.
    """
    if not has_verified_login_address(actor):
        raise AddressNotVerified(ADDRESS_NOT_VERIFIED_MESSAGE)
    with transaction.atomic():
        # FOR NO KEY UPDATE: two creates by one Account run in turn, so the
        # limit holds; Account deletion's FOR UPDATE waits or is waited on.
        locked = (
            get_user_model()
            .objects.select_for_update(no_key=True)
            .filter(pk=actor.pk, is_active=True)
            .first()
        )
        if locked is None:
            raise NotFound
        admin_count = ClubRole.objects.filter(
            account_id=locked.pk, role=ClubRoleKind.ADMIN
        ).count()
        if admin_count >= settings.CLUB_CREATE_LIMIT_PER_ACCOUNT:
            raise Conflict(CREATE_LIMIT_MESSAGE)
        club = Club(name=name, city=city, region=region)
        club.full_clean(exclude=["public_code"])
        club.save()
        record_event(action=AuditAction.CLUB_CREATE, target=club, actor=locked)
        save_access_row(
            ClubRole(club=club, account=locked, role=ClubRoleKind.ADMIN),
            actor=locked,
        )
    return club


def _admin_club(club_id: int, actor: "_User") -> Club:
    """Lock and return the Club when ``actor`` is its admin.

    Call inside a transaction. Holds the Account first, then locks the
    Club ``FOR UPDATE``, then checks the role, MFA, and ``is_active``
    from the database. ``NotFound`` otherwise.
    """
    if actor.pk is None or not hold_active_account(actor.pk):
        raise NotFound
    club = Club.objects.select_for_update().filter(pk=club_id).first()
    if club is None or not _is_club_admin(actor.pk, club.pk):
        raise NotFound
    return club


def _is_club_admin(account_id: int, club_id: int) -> bool:
    return bool(
        ClubRole.objects.filter(
            club_id=club_id,
            account_id=account_id,
            role=ClubRoleKind.ADMIN,
            account__is_active=True,
            account_id__in=accounts_with_mfa(),
        ).exists()
    )


def update_club(
    club_id: int,
    *,
    actor: "_User",
    name: str | None = None,
    city: str | None = None,
    region: str | None = None,
) -> bool:
    """Change the given profile fields; True when any changed.

    A field left as None is not changed. A change records one
    ``club.change`` event with the changed fields' old and new values;
    no change records nothing.
    """
    given = {"name": name, "city": city, "region": region}
    with transaction.atomic():
        club = _admin_club(club_id, actor)
        before = {field: getattr(club, field) for field in PROFILE_FIELDS}
        for field, value in given.items():
            if value is not None:
                setattr(club, field, value)
        club.full_clean(exclude=["public_code"])
        changed = [
            field for field in PROFILE_FIELDS if getattr(club, field) != before[field]
        ]
        if not changed:
            return False
        club.save(update_fields=changed)
        record_event(
            action=AuditAction.CLUB_CHANGE,
            target=club,
            actor=actor,
            before={field: before[field] for field in changed},
            after={field: getattr(club, field) for field in changed},
        )
    return True


def invite_admin(club_id: int, *, actor: "_User", email: str) -> None:
    """Invite ``email`` to be an admin of the Club.

    Raises ``InvalidInviteEmail`` before any lookup, ``NotFound`` for an
    actor who is not its admin, and ``Conflict`` when a new address would
    pass ``CLUB_OPEN_INVITES_LIMIT`` (re-inviting an address whose earlier
    invite expired counts as new). Otherwise returns the same way whether
    or not the address has an Account: an address that is an admin's
    login gets nothing; any other replaces its earlier invite to this
    Club, stores the new key's hash, records one ``club_invite.create``
    event (the Club's OPEN Code), and queues one email with the link.
    """
    address = normalized_invite_email(email)
    now = timezone.now()
    with transaction.atomic():
        club = _admin_club(club_id, actor)
        earlier = (
            ClubInvite.objects.select_for_update()
            .filter(club_id=club.pk, email=address)
            .first()
        )
        # Replacing a still open invite adds none; anything else adds one.
        if earlier is None or earlier.expires_at <= now:
            open_count = ClubInvite.objects.filter(
                club_id=club.pk, expires_at__gt=now
            ).count()
            if open_count >= settings.CLUB_OPEN_INVITES_LIMIT:
                raise Conflict(OPEN_INVITES_MESSAGE)
        if _admin_address(club.pk, address):
            return
        if earlier is not None:
            earlier.delete()
        key = new_invite_key()
        invite = ClubInvite.objects.create(
            club=club,
            email=address,
            key_hash=hash_invite_key(key),
            invited_by=actor,
            expires_at=now + timedelta(days=settings.CLUB_INVITE_DAYS),
        )
        record_event(
            action=AuditAction.CLUB_INVITE_CREATE,
            target=invite,
            actor=actor,
            # The OPEN Code, never the address (ADR 0009).
            after={"club": club.public_code},
        )
        queue_email(
            render_club_invite(
                address,
                club_code=club.public_code,
                key=key,
                expires_at=invite.expires_at,
            ),
            template=CLUB_INVITE,
        )


def _admin_address(club_id: int, address: str) -> bool:
    """Whether ``address`` is the login of one of the Club's admins."""
    try:
        account = get_user_model().objects.get_by_natural_key(address)
    except get_user_model().DoesNotExist:
        return False
    return bool(
        ClubRole.objects.filter(club_id=club_id, account_id=account.pk).exists()
    )


def accept_club_invite(key: str, *, actor: "_User") -> ClubRole:
    """Make ``actor`` an admin of the invite's Club; delete the invite.

    ``NotFound`` for an unknown, malformed, or expired key, another
    address, an unverified login address, an Account with no Player, and
    an invite whose sender is no longer an admin there (active, admin,
    with MFA). ``Conflict`` when ``actor`` is already an admin; the
    invite is deleted then too.
    """
    found = find_open_invite(ClubInvite.objects.all(), key)
    if found is None or actor.pk is None:
        raise NotFound
    already_admin = False
    with transaction.atomic():
        if not hold_active_account(actor.pk):
            raise NotFound
        # Account deletion and TOTP removal never lock the Club, so hold
        # the sender too, before the Club and the invite row (deletion
        # locks the sender, then deletes its invites).
        if not hold_active_account(found.invited_by_id):
            raise NotFound
        club = Club.objects.select_for_update().filter(pk=found.club_id).first()
        invite = find_open_invite(
            ClubInvite.objects.select_for_update().filter(pk=found.pk), key
        )
        if club is None or invite is None:
            raise NotFound
        if invite.invited_by_id != found.invited_by_id:
            raise NotFound
        if not invite_matches_account(invite, actor):
            raise NotFound
        # The invite gives no more authority than its sender holds now.
        if not _is_club_admin(invite.invited_by_id, club.pk):
            raise NotFound
        if not Player.objects.filter(account_id=actor.pk).exists():
            raise NotFound
        if ClubRole.objects.filter(club_id=club.pk, account_id=actor.pk).exists():
            invite.delete()
            already_admin = True
        else:
            role = ClubRole(club=club, account=actor, role=ClubRoleKind.ADMIN)
            save_access_row(role, actor=actor)
            invite.delete()
    if already_admin:
        raise Conflict(ALREADY_ADMIN_MESSAGE)
    return role


def list_admins(club_id: int, *, actor: "_User") -> list[ClubAdminView]:
    """The Club's admins, by Player OPEN Code (those with none last).

    Each with its Player's OPEN Code and display name (None when the
    Account has no Player, or the display name when it is inactive), and
    whether it is ``actor``.
    """
    with transaction.atomic():
        club = _admin_club(club_id, actor)
        roles = (
            ClubRole.objects.filter(club_id=club.pk)
            .select_related("account__player")
            .order_by(F("account__player__public_code").asc(nulls_last=True), "pk")
        )
        views = [_admin_view(role, actor) for role in roles]
    return views


def _admin_view(role: ClubRole, actor: "_User") -> ClubAdminView:
    account = role.account
    player = getattr(account, "player", None)
    return ClubAdminView(
        player=player.public_code if player is not None else None,
        display_name=(
            player.display_name if player is not None and account.is_active else None
        ),
        you=role.account_id == actor.pk,
    )


def remove_admin(club_id: int, player_code: str, *, actor: "_User") -> None:
    """Remove the admin whose Player has ``player_code``, yourself included.

    ``NotFound`` when no admin of this Club has that Player, and
    ``Conflict`` when it would leave no active admin. Deletes the invites
    that admin sent to this Club in the same transaction.
    """
    with transaction.atomic():
        club = _admin_club(club_id, actor)
        role = (
            ClubRole.objects.select_for_update(of=("self",))
            .filter(club_id=club.pk, account__player__public_code=player_code)
            .first()
        )
        if role is None:
            raise NotFound
        others = (
            ClubRole.objects.filter(
                club_id=club.pk, role=ClubRoleKind.ADMIN, account__is_active=True
            )
            .exclude(pk=role.pk)
            .count()
        )
        if others == 0:
            raise Conflict(LAST_ADMIN_MESSAGE)
        ClubInvite.objects.filter(
            club_id=club.pk, invited_by_id=role.account_id
        ).delete()
        delete_access_row(role, actor=actor)


def list_grants(club_id: int, *, actor: "_User") -> list[ClubGrantView]:
    """The Organizers the Club allows, by Organizer OPEN Code."""
    with transaction.atomic():
        club = _admin_club(club_id, actor)
        grants = (
            ClubOrganizerGrant.objects.filter(club_id=club.pk)
            .select_related("organizer")
            .only("created_at", "organizer__public_code")
            .order_by("organizer__public_code")
        )
        views = [
            ClubGrantView(
                organizer=grant.organizer.public_code, created_at=grant.created_at
            )
            for grant in grants
        ]
    return views


def _organizer_id(raw: str) -> int | None:
    """The row id of the Organizer ``raw`` names, in any form
    ``normalize`` accepts, or None."""
    code = normalize(raw, CodeType.ORGANIZER)
    if code is None:
        return None
    found = Organizer.objects.filter(public_code=code).values_list("pk", flat=True)
    return found.first()


def _hold_organizer(raw: str) -> int | None:
    """Like ``_organizer_id``, holding the row ``FOR KEY SHARE`` until the
    transaction ends, so it cannot be deleted before a grant names it.

    A delete that held the row first is waited for, and then the row is
    gone: None. Django has no share modes, so this is raw SQL, as in
    ``apps.accounts.locks``.
    """
    code = normalize(raw, CodeType.ORGANIZER)
    if code is None:
        return None
    table = connection.ops.quote_name(Organizer._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id FROM {table} WHERE public_code = %s FOR KEY SHARE", [code]
        )
        row = cursor.fetchone()
    return int(row[0]) if row else None


def grant_organizer(
    club_id: int, organizer_code: str, *, actor: "_User"
) -> ClubOrganizerGrant:
    """Allow the Organizer with ``organizer_code`` to name the Club.

    The Organizer need not be verified: the grant is consent, not trust.
    ``NotFound`` for an unknown or malformed code, ``Conflict`` when the
    Club already allows it. Records ``club_organizer_grant.add``.
    """
    with transaction.atomic():
        club = _admin_club(club_id, actor)
        organizer_id = _hold_organizer(organizer_code)
        if organizer_id is None:
            raise NotFound
        if ClubOrganizerGrant.objects.filter(
            club_id=club.pk, organizer_id=organizer_id
        ).exists():
            raise Conflict(ALREADY_GRANTED_MESSAGE)
        grant = ClubOrganizerGrant(club=club, organizer_id=organizer_id)
        save_access_row(grant, actor=actor)
    return grant


def remove_grant(club_id: int, organizer_code: str, *, actor: "_User") -> None:
    """Stop allowing the Organizer with ``organizer_code`` to name the Club.

    Events and matches already recorded keep the Club; a new Event or
    match naming it is refused from then on. ``NotFound`` when the Club
    has no such grant. Records ``club_organizer_grant.remove``.
    """
    with transaction.atomic():
        club = _admin_club(club_id, actor)
        organizer_id = _organizer_id(organizer_code)
        grant = (
            ClubOrganizerGrant.objects.select_for_update(of=("self",))
            .filter(club_id=club.pk, organizer_id=organizer_id)
            .first()
            if organizer_id is not None
            else None
        )
        if grant is None:
            raise NotFound
        delete_access_row(grant, actor=actor)

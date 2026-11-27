"""Organizer self-service (#229, ADR 0054).

An Account sets up its own Organizer, invites members, and asks to be
verified. None of this gives submission authority: only
``apps.clubs.services.verify_organizer`` does (ADR 0021), so an
Organizer made here stays unverified until a moderator verifies it.

- ``create_organizer``: an active Account whose login address is
  verified creates an unverified Organizer and becomes its only member,
  as admin. Past ``ORGANIZER_CREATE_LIMIT_PER_ACCOUNT`` admin
  memberships it is refused.
- Every other function acts for an admin member: an active Account with
  MFA (ADR 0043) whose membership of that Organizer is admin, read from
  the database. Anyone else gets ``NotFound``, the same as an unknown
  Organizer, so the answer never says who is a member.
- ``rename_organizer``, ``invite_member``, ``list_members``,
  ``change_member_role``, ``remove_member``, ``request_verification``,
  and ``list_club_grants`` (the Clubs that granted it, #230).
- ``accept_invite``: the invited Account, signed in with the invited
  login address, verified, and with a Player, joins with the invited
  role, while the invite's sender still has admin authority there
  (active, admin, with MFA, no New Member Hold). Joining a verified
  Organizer starts a New Member Hold of
  ``ORGANIZER_NEW_MEMBER_HOLD_HOURS`` (#232, ADR 0057): until it ends the
  member has no Organizer or admin authority. Demoting or removing an admin deletes the invites it
  sent there in the same transaction. Every refusal is ``NotFound``.

On these paths an Organizer always keeps one active admin with MFA
(``Conflict`` otherwise, #232), and removing TOTP refuses to leave one
without (``apps.accounts.mfa_views``). Account deletion and the staff admin can still leave an
Organizer without an admin; staff repair it. A verified Organizer is not
renamed here (``Conflict``). Once an Organizer is verified, an admin's
invite gives the new member trusted-submitter authority (ADR 0021) once
its New Member Hold ends and while the member has MFA, with no moderator
review of that member; a moderator may end the hold early
(``apps.clubs.services.end_member_holds``).
Each change records its Audit Events in its own transaction: never the
Organizer's name, an address, or a statement (ADR 0009).

Lock order: the acting Account (``FOR KEY SHARE`` through
``hold_active_account``, ``FOR NO KEY UPDATE`` in ``create_organizer``),
then the Organizer row (``FOR UPDATE``), then invite and membership
rows, as Account deletion takes the Account, then the Organizers, then
their rows (ADR 0048), and ``verify_organizer`` takes the Organizer
before its requests. Two concurrent demotions both wait on the
Organizer lock, so the last admin count cannot pass twice.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.addresses import has_verified_login_address
from apps.accounts.locks import hold_active_account
from apps.accounts.mfa import accounts_with_mfa
from apps.audit.access import delete_access_row, save_access_row
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.clubs.emails import ORGANIZER_INVITE, render_organizer_invite
from apps.clubs.invites import (
    find_open_invite,
    hash_invite_key,
    invite_matches_account,
    new_invite_key,
    normalized_invite_email,
)
from apps.clubs.models import (
    ADMIN_ACCOUNT_MESSAGE,
    ClubOrganizerGrant,
    MemberRole,
    Organizer,
    OrganizerInvite,
    OrganizerMember,
    OrganizerVerificationRequest,
    admin_account_refused,
    not_held,
)
from apps.emails.services import queue_email
from apps.players.models import Player

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

# Fixed texts, never a submitted value.
ADDRESS_NOT_VERIFIED_MESSAGE: Final = (
    "Verify your email address before creating an organizer."
)
CREATE_LIMIT_MESSAGE: Final = (
    "This account is already an admin of as many organizers as it may create."
)
OPEN_INVITES_MESSAGE: Final = (
    "This organizer has too many open invites. Wait until some are accepted or expire."
)
LAST_ADMIN_MESSAGE: Final = (
    "An organizer must keep at least one admin with two-factor authentication."
)
ALREADY_MEMBER_MESSAGE: Final = "You are already a member of this organizer."
ALREADY_VERIFIED_MESSAGE: Final = "This organizer is already verified."
VERIFIED_RENAME_MESSAGE: Final = (
    "A verified organizer cannot be renamed here. Ask a moderator to unverify it first."
)
REQUEST_OPEN_MESSAGE: Final = "This organizer already has an open verification request."
ADMIN_ACCOUNT_CONFLICT_MESSAGE: Final = ADMIN_ACCOUNT_MESSAGE


class NotFound(LookupError):
    """The Organizer, Club, invite, member, Club admin, or grant is unknown,
    or the actor may not act on it. One answer for every cause: the API's
    404. Club self-service (``apps.clubs.club_self_service``) raises it
    too."""


class Conflict(ValueError):
    """The change would break a rule (the API's 409). Fixed text."""


class AddressNotVerified(PermissionError):
    """The Account's login address is not verified (the API's 403).

    ``message`` is fixed text; the Club create passes its own (#230).
    """

    def __init__(self, message: str = ADDRESS_NOT_VERIFIED_MESSAGE) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class MemberView:
    """One member as an admin sees it: never an email or a row id."""

    player: str | None
    display_name: str | None
    role: MemberRole
    you: bool


@dataclass(frozen=True)
class OrganizerClubGrantView:
    """A Club that granted the Organizer a Club Organizer Grant (#230), as
    the Organizer's admin members see it: the Club's public profile and
    when it granted. Never a row id."""

    club: str
    name: str
    city: str
    region: str
    created_at: datetime


def create_organizer(*, actor: "_User", name: str) -> Organizer:
    """Create an unverified Organizer with ``actor`` as its only admin.

    Raises ``AddressNotVerified`` before any write, ``Conflict`` at the
    per-Account limit, ``NotFound`` for an Account deleted meanwhile, and
    Django's ``ValidationError`` for a name the model refuses.
    """
    if not has_verified_login_address(actor):
        raise AddressNotVerified
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
        admin_count = OrganizerMember.objects.filter(
            account_id=locked.pk, role=MemberRole.ADMIN
        ).count()
        if admin_count >= settings.ORGANIZER_CREATE_LIMIT_PER_ACCOUNT:
            raise Conflict(CREATE_LIMIT_MESSAGE)
        organizer = Organizer(name=name)
        organizer.full_clean(exclude=["public_code"])
        organizer.save()
        # No values: the name may be a person's (ADR 0009).
        record_event(
            action=AuditAction.ORGANIZER_CREATE, target=organizer, actor=locked
        )
        save_access_row(
            OrganizerMember(organizer=organizer, account=locked, role=MemberRole.ADMIN),
            actor=locked,
        )
    return organizer


def _admin_organizer(organizer_id: int, actor: "_User") -> Organizer:
    """Lock and return the Organizer when ``actor`` is its admin member.

    Call inside a transaction. Holds the Account first, then locks the
    Organizer ``FOR UPDATE``, then checks the membership, MFA, no New
    Member Hold in force, and ``is_active`` from the database
    (``_is_admin``). ``NotFound`` otherwise.
    """
    if actor.pk is None or not hold_active_account(actor.pk):
        raise NotFound
    organizer = Organizer.objects.select_for_update().filter(pk=organizer_id).first()
    if organizer is None or not _is_admin(actor.pk, organizer.pk):
        raise NotFound
    return organizer


def _is_admin(account_id: int, organizer_id: int) -> bool:
    return bool(
        OrganizerMember.objects.filter(
            # A held member has no admin authority either (#232).
            not_held(),
            organizer_id=organizer_id,
            account_id=account_id,
            role=MemberRole.ADMIN,
            account__is_active=True,
            account_id__in=accounts_with_mfa(),
        ).exists()
    )


def rename_organizer(organizer_id: int, *, actor: "_User", name: str) -> bool:
    """Change the Organizer's name; True when it changed.

    An unchanged name records nothing. A change records one
    ``organizer.change`` event naming the field, never its value.
    ``Conflict`` while the Organizer is verified, checked under its lock:
    a moderator verified it under its name, so a new name needs that
    review again (unverify first, or staff rename it in the admin).
    """
    with transaction.atomic():
        organizer = _admin_organizer(organizer_id, actor)
        if organizer.is_verified:
            raise Conflict(VERIFIED_RENAME_MESSAGE)
        old = organizer.name
        organizer.name = name
        organizer.full_clean(exclude=["public_code"])
        if organizer.name == old:
            return False
        organizer.save(update_fields=["name"])
        record_event(
            action=AuditAction.ORGANIZER_CHANGE,
            target=organizer,
            actor=actor,
            after={"fields": ["name"]},
        )
    return True


def invite_member(organizer_id: int, *, actor: "_User", email: str, role: str) -> None:
    """Invite ``email`` to join the Organizer as ``role``.

    Raises ``InvalidInviteEmail`` before any lookup, ``NotFound`` for an
    actor who is not its admin, and ``Conflict`` when a new address would
    pass ``ORGANIZER_OPEN_INVITES_LIMIT`` (re-inviting an address whose
    earlier invite expired counts as new). Otherwise returns the same way
    whether or not the address has an Account: an address that is a
    member's login gets nothing; any other replaces its earlier invite
    to this Organizer, stores the new key's hash, records one
    ``organizer_invite.create`` event (the OPEN Code and the role), and
    queues one email with the link.
    """
    address = normalized_invite_email(email)
    role = MemberRole(role)
    now = timezone.now()
    with transaction.atomic():
        organizer = _admin_organizer(organizer_id, actor)
        earlier = (
            OrganizerInvite.objects.select_for_update()
            .filter(organizer_id=organizer.pk, email=address)
            .first()
        )
        # Replacing a still open invite adds none; anything else adds one.
        if earlier is None or earlier.expires_at <= now:
            open_count = OrganizerInvite.objects.filter(
                organizer_id=organizer.pk, expires_at__gt=now
            ).count()
            if open_count >= settings.ORGANIZER_OPEN_INVITES_LIMIT:
                raise Conflict(OPEN_INVITES_MESSAGE)
        if _member_address(organizer.pk, address):
            return
        if earlier is not None:
            earlier.delete()
        key = new_invite_key()
        invite = OrganizerInvite.objects.create(
            organizer=organizer,
            email=address,
            key_hash=hash_invite_key(key),
            role=role,
            invited_by=actor,
            expires_at=now + timedelta(days=settings.ORGANIZER_INVITE_DAYS),
        )
        record_event(
            action=AuditAction.ORGANIZER_INVITE_CREATE,
            target=invite,
            actor=actor,
            # The OPEN Code and the role, never the address (ADR 0009).
            after={"organizer": organizer.public_code, "role": str(role)},
        )
        queue_email(
            render_organizer_invite(
                address,
                organizer_code=organizer.public_code,
                role=role,
                key=key,
                expires_at=invite.expires_at,
            ),
            template=ORGANIZER_INVITE,
        )


def _member_address(organizer_id: int, address: str) -> bool:
    """Whether ``address`` is the login of one of the Organizer's members."""
    try:
        account = get_user_model().objects.get_by_natural_key(address)
    except get_user_model().DoesNotExist:
        return False
    return bool(
        OrganizerMember.objects.filter(
            organizer_id=organizer_id, account_id=account.pk
        ).exists()
    )


def accept_invite(key: str, *, actor: "_User") -> OrganizerMember:
    """Make ``actor`` a member with the invite's role; delete the invite.

    ``NotFound`` for an unknown, malformed, or expired key, another
    address, an unverified login address, an Account with no Player, and
    the verifier of a verified Organizer (``OrganizerMember.save``), and an
    invite whose sender no longer has admin authority there (active,
    admin, with MFA).
    ``Conflict`` when ``actor`` is already a member; the invite is
    deleted then too.
    """
    found = find_open_invite(OrganizerInvite.objects.all(), key)
    if found is None or actor.pk is None:
        raise NotFound
    already_member = False
    with transaction.atomic():
        if not hold_active_account(actor.pk):
            raise NotFound
        organizer = (
            Organizer.objects.select_for_update().filter(pk=found.organizer_id).first()
        )
        invite = find_open_invite(
            OrganizerInvite.objects.select_for_update().filter(pk=found.pk), key
        )
        if organizer is None or invite is None:
            raise NotFound
        if not invite_matches_account(invite, actor):
            raise NotFound
        if not _sender_still_admin(invite):
            # The invite gives no more authority than its sender holds now.
            raise NotFound
        if not Player.objects.filter(account_id=actor.pk).exists():
            raise NotFound
        if OrganizerMember.objects.filter(
            organizer_id=organizer.pk, account_id=actor.pk
        ).exists():
            invite.delete()
            already_member = True
        else:
            membership = OrganizerMember(
                organizer=organizer,
                account=actor,
                role=invite.role,
                hold_ends_at=_hold_ends_at(organizer),
            )
            try:
                save_access_row(membership, actor=actor)
            except ValidationError:
                # The verifier of a verified Organizer cannot join it.
                raise NotFound from None
            invite.delete()
    if already_member:
        raise Conflict(ALREADY_MEMBER_MESSAGE)
    return membership


def _hold_ends_at(organizer: Organizer) -> datetime | None:
    """When a member joining ``organizer`` now gets authority (#232, ADR
    0057): ``ORGANIZER_NEW_MEMBER_HOLD_HOURS`` from now while it is
    verified (read under the Organizer lock), None when it is not or the
    hold is off."""
    hours = settings.ORGANIZER_NEW_MEMBER_HOLD_HOURS
    if not organizer.is_verified or hours <= 0:
        return None
    return timezone.now() + timedelta(hours=hours)


def _sender_still_admin(invite: OrganizerInvite) -> bool:
    """Whether the invite's sender still has admin authority there: the
    same rule as ``_is_admin`` (active, admin role, MFA, no New Member
    Hold in force). Call under the Organizer lock, which every membership
    change takes first."""
    return _is_admin(invite.invited_by_id, invite.organizer_id)


def _delete_sent_invites(organizer: Organizer, account_id: int) -> None:
    """Delete the invites ``account_id`` sent to ``organizer``. Call in the
    transaction that demotes or removes that admin, under the Organizer
    lock, so none outlives the sender's authority."""
    OrganizerInvite.objects.filter(
        organizer_id=organizer.pk, invited_by_id=account_id
    ).delete()


def list_members(organizer_id: int, *, actor: "_User") -> list[MemberView]:
    """The Organizer's members, admins first, then by Player OPEN Code.

    Each with its Player's OPEN Code and display name (None when the
    Account has no Player, or the display name when it is inactive), its
    role, and whether it is ``actor``.
    """
    with transaction.atomic():
        organizer = _admin_organizer(organizer_id, actor)
        memberships = (
            OrganizerMember.objects.filter(organizer_id=organizer.pk)
            .select_related("account__player")
            .order_by(
                "role",
                F("account__player__public_code").asc(nulls_last=True),
                "pk",
            )
        )
        views = [_member_view(membership, actor) for membership in memberships]
    return views


def _member_view(membership: OrganizerMember, actor: "_User") -> MemberView:
    """``membership`` as an admin sees it. Load it with ``account__player``."""
    account = membership.account
    player = getattr(account, "player", None)
    return MemberView(
        player=player.public_code if player is not None else None,
        display_name=(
            player.display_name if player is not None and account.is_active else None
        ),
        role=MemberRole(membership.role),
        you=membership.account_id == actor.pk,
    )


def _target_membership(organizer: Organizer, player_code: str) -> OrganizerMember:
    membership = (
        OrganizerMember.objects.select_for_update(of=("self",))
        .filter(organizer_id=organizer.pk, account__player__public_code=player_code)
        .first()
    )
    if membership is None:
        raise NotFound
    return membership


def _keeps_an_admin(organizer: Organizer, leaving: OrganizerMember) -> bool:
    """Whether an active admin with MFA and no New Member Hold in force,
    other than ``leaving``, stays (#232). Call under the Organizer lock, so
    concurrent changes count in turn."""
    others = (
        OrganizerMember.objects.filter(
            not_held(),
            organizer_id=organizer.pk,
            role=MemberRole.ADMIN,
            account__is_active=True,
            account_id__in=accounts_with_mfa(),
        )
        .exclude(pk=leaving.pk)
        .count()
    )
    return others > 0


def change_member_role(
    organizer_id: int, player_code: str, *, actor: "_User", role: str
) -> MemberView:
    """Give the member whose Player has ``player_code`` the role ``role``.

    Returns the changed member, read in the change's transaction, so an
    admin who demoted themselves gets it too. ``NotFound`` when no member
    of this Organizer has that Player, and ``Conflict`` when it would
    leave no active admin with MFA and no New Member Hold in force, or
    would make an admin of an Account
    that is inactive or whose login address is not verified (#232). An
    unchanged role records nothing.
    """
    role = MemberRole(role)
    with transaction.atomic():
        organizer = _admin_organizer(organizer_id, actor)
        membership = _target_membership(organizer, player_code)
        if (
            membership.role == MemberRole.ADMIN
            and role != MemberRole.ADMIN
            and not _keeps_an_admin(organizer, membership)
        ):
            raise Conflict(LAST_ADMIN_MESSAGE)
        if (
            role == MemberRole.ADMIN
            and membership.role != MemberRole.ADMIN
            and admin_account_refused(membership.account_id)
        ):
            # An admin needs an active Account with a verified address (#232).
            raise Conflict(ADMIN_ACCOUNT_CONFLICT_MESSAGE)
        if membership.role == MemberRole.ADMIN and role != MemberRole.ADMIN:
            _delete_sent_invites(organizer, membership.account_id)
        membership.role = role
        save_access_row(membership, actor=actor)
        changed = OrganizerMember.objects.select_related("account__player").get(
            pk=membership.pk
        )
        return _member_view(changed, actor)


def remove_member(organizer_id: int, player_code: str, *, actor: "_User") -> None:
    """Remove the member whose Player has ``player_code``.

    ``NotFound`` when no member of this Organizer has that Player, and
    ``Conflict`` when it would leave no active admin with MFA and no New
    Member Hold in force (#232).
    """
    with transaction.atomic():
        organizer = _admin_organizer(organizer_id, actor)
        membership = _target_membership(organizer, player_code)
        if membership.role == MemberRole.ADMIN and not _keeps_an_admin(
            organizer, membership
        ):
            raise Conflict(LAST_ADMIN_MESSAGE)
        _delete_sent_invites(organizer, membership.account_id)
        delete_access_row(membership, actor=actor)


def request_verification(
    organizer_id: int, *, actor: "_User", statement: str
) -> OrganizerVerificationRequest:
    """Ask moderators to verify the Organizer, with a short ``statement``.

    ``Conflict`` when it is already verified or has an open request.
    Records one ``organizer_verification_request.create`` event with the
    Organizer's OPEN Code only. Changes nothing about verification.
    """
    with transaction.atomic():
        organizer = _admin_organizer(organizer_id, actor)
        if organizer.is_verified:
            raise Conflict(ALREADY_VERIFIED_MESSAGE)
        if OrganizerVerificationRequest.objects.filter(
            organizer_id=organizer.pk, closed_at__isnull=True
        ).exists():
            raise Conflict(REQUEST_OPEN_MESSAGE)
        request = OrganizerVerificationRequest.objects.create(
            organizer=organizer, statement=statement
        )
        record_event(
            action=AuditAction.ORGANIZER_VERIFICATION_REQUEST_CREATE,
            target=request,
            actor=actor,
            after={"organizer": organizer.public_code},
        )
    return request


def list_club_grants(
    organizer_id: int, *, actor: "_User"
) -> list[OrganizerClubGrantView]:
    """The Clubs that granted the Organizer a Club Organizer Grant (#230),
    by Club OPEN Code. For an admin member (``_admin_organizer``)."""
    with transaction.atomic():
        organizer = _admin_organizer(organizer_id, actor)
        grants = (
            ClubOrganizerGrant.objects.filter(organizer_id=organizer.pk)
            .select_related("club")
            .order_by("club__public_code")
        )
        views = [
            OrganizerClubGrantView(
                club=grant.club.public_code,
                name=grant.club.name,
                city=grant.club.city,
                region=grant.club.region,
                created_at=grant.created_at,
            )
            for grant in grants
        ]
    return views

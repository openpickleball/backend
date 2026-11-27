"""Security notices to Organizer and Club admins (#232, ADR 0057).

A change to who may act for an Organizer or a Club, or to whether an
Organizer is trusted, tells that Organizer's or Club's other admins by
email, so a takeover of one admin's Account is seen by the rest:

- Organizer Members added, removed, or given another role, Club admins
  added or removed, Club Organizer Grants made or removed (to the Club's
  admins), and Organizer Application Grants made or removed (to the
  Organizer's admins). ``apps.audit.access`` calls each row's
  ``access_notice`` after it records the add, change, or removal, so the
  admin, the self-service services, and Account deletion all send them.
  A change of nothing but a New Member Hold sends none, and
  ``record_cascaded_removals`` sends none (the parent row is going).
- An Organizer verified or unverified (``apps.clubs.services``).
- A member removing their last two-factor method
  (``apps.accounts.mfa_views``), for each Organizer it is a member of and
  each Club it is an admin of.

Recipients are every active admin of the Organizer (members with role
admin) or the Club (admin Club Roles) when the change commits, except
the acting Account and the Account the change is about. On a removal, a
role change away from admin, or a staff move of its admin row to
another Account, that Account is told too when it is active and is not
the actor, since at commit it is no longer an admin. Each gets one
email through ``queue_email()``, registered with ``transaction.on_commit``
in the change's transaction, so a rolled-back change queues nothing.

A notice names the Organizer's or Club's OPEN Code, the change, and
where it applies a role, an Organizer's OPEN Code, and the time a New
Member Hold ends (UTC). Never an email address, a person's name, an
Organizer's or Club's name, or a client id. A failure while building or
queuing a notice is logged with row ids and the error class only and
never fails the change.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from typing import Final

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q

from apps.clubs.emails import SECURITY_NOTICE, render_security_notice
from apps.clubs.models import ClubRole, ClubRoleKind, MemberRole, OrganizerMember
from apps.emails.services import queue_email

logger = logging.getLogger(__name__)

ORGANIZER: Final = "organizer"
CLUB: Final = "club"


class NoticeKind(StrEnum):
    """What changed."""

    MEMBER_ADDED = "member_added"
    MEMBER_REMOVED = "member_removed"
    MEMBER_ROLE_CHANGED = "member_role_changed"
    CLUB_ADMIN_ADDED = "club_admin_added"
    CLUB_ADMIN_REMOVED = "club_admin_removed"
    CLUB_GRANT_ADDED = "club_grant_added"
    CLUB_GRANT_REMOVED = "club_grant_removed"
    APPLICATION_GRANT_ADDED = "application_grant_added"
    APPLICATION_GRANT_REMOVED = "application_grant_removed"
    ORGANIZER_VERIFIED = "organizer_verified"
    ORGANIZER_UNVERIFIED = "organizer_unverified"
    MFA_REMOVED = "mfa_removed"


# Fixed sentences: only OPEN Codes, a role, and a time are filled in.
_SUMMARIES: Final[dict[NoticeKind, str]] = {
    NoticeKind.MEMBER_ADDED: "A member was added to organizer {code} as {role}.",
    NoticeKind.MEMBER_REMOVED: "A member was removed from organizer {code}.",
    NoticeKind.MEMBER_ROLE_CHANGED: (
        "A member's role at organizer {code} was changed to {role}."
    ),
    NoticeKind.CLUB_ADMIN_ADDED: "An admin was added to club {code}.",
    NoticeKind.CLUB_ADMIN_REMOVED: "An admin was removed from club {code}.",
    NoticeKind.CLUB_GRANT_ADDED: (
        "Club {code} now allows organizer {organizer} to name it on its events "
        "and matches."
    ),
    NoticeKind.CLUB_GRANT_REMOVED: (
        "Club {code} no longer allows organizer {organizer} to name it on its "
        "events and matches."
    ),
    NoticeKind.APPLICATION_GRANT_ADDED: (
        "A partner application was given permission to submit and confirm "
        "matches for organizer {code}."
    ),
    NoticeKind.APPLICATION_GRANT_REMOVED: (
        "A partner application no longer has permission to act for organizer {code}."
    ),
    NoticeKind.ORGANIZER_VERIFIED: (
        "Organizer {code} was verified by an OPEN moderator. Matches its "
        "members record for its events now count."
    ),
    NoticeKind.ORGANIZER_UNVERIFIED: (
        "Organizer {code} is no longer verified. Its members can no longer "
        "record counted matches for it."
    ),
    NoticeKind.MFA_REMOVED: (
        "A member of {scope} {code} removed their last two-factor method. "
        "They cannot act for the {scope} until they set one up again."
    ),
}
_HOLD: Final = (
    " The new member cannot act for the organizer until {hold_ends_at}, "
    "unless an OPEN moderator ends the wait early."
)
_FOLLOW_UP: Final = (
    "If you did not expect this change, sign in to OPEN and check who can act "
    "for the {scope}, and change your password if you think someone else used "
    "your account."
)
_ROLE_LABELS: Final[dict[str, str]] = {
    MemberRole.ADMIN: "admin",
    MemberRole.MATCH_MANAGER: "match manager",
}


@dataclass(frozen=True)
class SecurityNotice:
    """One change to tell an Organizer's or Club's admins about.

    ``scope_id`` is the Organizer's or Club's row id, ``code`` its OPEN
    Code. ``exclude`` holds the Account ids that get no copy: the actor
    and the Account the change is about. ``told`` holds the Account ids
    that get a copy though they are no longer admins: a removed or demoted
    admin, never the actor (``subject_told``). Plain values only, read
    when the change was made, since the rows may be gone at commit.
    """

    kind: NoticeKind
    scope: str
    scope_id: int
    code: str
    role: str = ""
    organizer_code: str = ""
    hold_ends_at: datetime | None = None
    exclude: frozenset[int] = field(default_factory=frozenset)
    told: frozenset[int] = field(default_factory=frozenset)

    def summary(self) -> str:
        text = _SUMMARIES[self.kind].format(
            code=self.code,
            scope=self.scope,
            role=_ROLE_LABELS.get(self.role, self.role),
            organizer=self.organizer_code,
        )
        if self.kind == NoticeKind.MEMBER_ADDED and self.hold_ends_at is not None:
            text += _HOLD.format(
                hold_ends_at=self.hold_ends_at.astimezone(UTC).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            )
        return text

    def follow_up(self) -> str:
        return _FOLLOW_UP.format(scope=self.scope)


def account_ids(*accounts: object) -> frozenset[int]:
    """The row ids of the saved, signed-in Accounts among ``accounts``
    (an actor may be None or anonymous)."""
    ids: set[int] = set()
    for account in accounts:
        if account is None:
            continue
        if isinstance(account, int):
            ids.add(account)
            continue
        pk = getattr(account, "pk", None)
        if isinstance(pk, int) and getattr(account, "is_authenticated", False):
            ids.add(pk)
    return frozenset(ids)


def subject_told(actor: object, account_id: object) -> frozenset[int]:
    """``account_id`` as the removed or demoted admin to tell, unless it is
    the actor (an admin who leaves is not told) or not an id."""
    if not isinstance(account_id, int) or account_id in account_ids(actor):
        return frozenset()
    return frozenset({account_id})


def schedule_notice(notice: SecurityNotice) -> None:
    """Queue ``notice`` once the change's transaction commits."""
    try:
        transaction.on_commit(partial(send_notice, notice))
    except Exception as exc:
        # The broad catch is safe: a notice is never worth failing the
        # change over. Only the class is logged.
        _failed(notice, exc)


def send_notice(notice: SecurityNotice) -> int:
    """Queue one email per recipient of ``notice``. Returns how many.

    Runs after the change committed, so the recipients are the admins at
    that moment.
    """
    try:
        recipients = list(_recipients(notice))
    except Exception as exc:
        # The broad catch is safe: the change has committed, and a notice
        # is never worth failing the caller over.
        _failed(notice, exc)
        return 0
    sent = 0
    for address in recipients:
        try:
            queue_email(
                render_security_notice(
                    address,
                    scope=notice.scope,
                    code=notice.code,
                    summary=notice.summary(),
                    follow_up=notice.follow_up(),
                ),
                template=SECURITY_NOTICE,
            )
        except Exception as exc:
            # Per recipient, so one failure never drops the others.
            _failed(notice, exc)
            continue
        sent += 1
    return sent


def _recipients(notice: SecurityNotice) -> list[str]:
    """The login addresses of the active admins and of the active Accounts
    in ``told``, by Account id, without ``exclude`` (``told`` wins)."""
    if notice.scope == ORGANIZER:
        admins = Q(
            pk__in=OrganizerMember.objects.filter(
                organizer_id=notice.scope_id, role=MemberRole.ADMIN
            ).values("account_id")
        )
    else:
        admins = Q(
            pk__in=ClubRole.objects.filter(
                club_id=notice.scope_id, role=ClubRoleKind.ADMIN
            ).values("account_id")
        )
    users = get_user_model().objects.filter(
        admins | Q(pk__in=notice.told), is_active=True
    )
    emails: list[str] = list(
        users.exclude(pk__in=notice.exclude - notice.told)
        .order_by("pk")
        .values_list("email", flat=True)
    )
    return emails


def _failed(notice: SecurityNotice, exc: Exception) -> None:
    # Row ids and the class only: an error's text can quote an address.
    logger.warning(
        "Could not queue security notices for %s %s (%s).",
        notice.scope,
        notice.scope_id,
        type(exc).__name__,
    )


def schedule_mfa_removed(account_id: int) -> None:
    """Tell the other admins of every Organizer ``account_id`` is a member
    of, and every Club it is an admin of, that it removed its last
    two-factor method. Call in the removal's transaction."""
    try:
        memberships = OrganizerMember.objects.filter(
            account_id=account_id
        ).select_related("organizer")
        notices = [
            SecurityNotice(
                kind=NoticeKind.MFA_REMOVED,
                scope=ORGANIZER,
                scope_id=membership.organizer_id,
                code=membership.organizer.public_code,
                exclude=frozenset({account_id}),
            )
            for membership in memberships.order_by("organizer_id")
        ]
        roles = ClubRole.objects.filter(
            account_id=account_id, role=ClubRoleKind.ADMIN
        ).select_related("club")
        notices += [
            SecurityNotice(
                kind=NoticeKind.MFA_REMOVED,
                scope=CLUB,
                scope_id=role.club_id,
                code=role.club.public_code,
                exclude=frozenset({account_id}),
            )
            for role in roles.order_by("club_id")
        ]
    except Exception as exc:
        logger.warning(
            "Could not queue security notices for account %s (%s).",
            account_id,
            type(exc).__name__,
        )
        return
    for notice in notices:
        schedule_notice(notice)

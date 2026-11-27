"""Verify and unverify Organizers.

A verified Organizer's submissions count as confirmed (CLAUDE.md, hard
rule 8), so these are the only ways to change ``verified_at`` and
``verified_by``. Each refuses, before any write, an actor that is not an
active Account with ``clubs.verify_organizer``, or that is a member of the
Organizer. A change records one Audit Event in the same transaction. An
unverify that changes the status also closes every open Organizer Report
on it (``apps.clubs.reports``) in that transaction, with one
``organizer_report.close`` Audit Event each, and a verify that changes
the status closes the Organizer's open Verification Request
(``apps.clubs.verification_requests``) with one
``organizer_verification_request.close`` event, rule ``verified``. A
call that would change
nothing records nothing. Each call sets
``organizer.verification_changed`` to whether that call changed the
status, so a caller (the admin) reports only its own changes.

``end_member_holds`` lets a moderator end an Organizer's New Member Holds
early (#232, ADR 0057), with a required reason in each event.
"""

from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models.functions import Now
from django.utils import timezone

from apps.audit.access import save_access_row
from apps.audit.admin_actions import REASON_MAX_LENGTH
from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot
from apps.clubs.models import VERIFY_PERMISSION, Organizer, OrganizerMember
from apps.clubs.reports import (
    RULE_ORGANIZER_UNVERIFIED,
    close_open_reports,
    may_close,
)
from apps.clubs.verification_requests import close_open_verification_requests

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

_FIELDS = ["verified_at", "verified_by"]


class NotAllowedToVerify(PermissionError):
    """The actor may not verify or unverify this Organizer."""


class NotAllowedToEndHolds(PermissionError):
    """The actor may not end this Organizer's New Member Holds."""


def end_member_holds(organizer: Organizer, *, actor: "_User", reason: str) -> int:
    """End every New Member Hold still in force on ``organizer`` (#232,
    ADR 0057) and return how many.

    For an actor ``apps.clubs.reports.may_close`` allows (active, with
    ``clubs.verify_organizer``, not a member of the Organizer):
    ``NotAllowedToEndHolds`` otherwise, and ``ValueError`` for a blank
    reason or one over ``REASON_MAX_LENGTH``, both before any write. Under
    the Organizer lock (``FOR UPDATE``) first, each held member's
    ``hold_ends_at`` is cleared through ``save_access_row``, which records
    one ``organizer_member.change`` event with ``reason``.
    """
    if not may_close(actor, organizer.pk):
        raise NotAllowedToEndHolds(
            "Only an active Account with permission that is not a member can "
            "end new member holds."
        )
    reason = reason.strip()
    if not reason or len(reason) > REASON_MAX_LENGTH:
        raise ValueError("Give a reason of at most 1000 characters.")
    ended = 0
    with transaction.atomic():
        locked = Organizer.objects.select_for_update().get(pk=organizer.pk)
        held = OrganizerMember.objects.filter(
            organizer_id=locked.pk, hold_ends_at__gt=Now()
        ).order_by("pk")
        for member in held:
            member.hold_ends_at = None
            save_access_row(member, actor=actor, reason=reason)
            ended += 1
    return ended


def verify_organizer(
    organizer: Organizer, *, actor: "_User", reason: str = ""
) -> Organizer:
    """Mark ``organizer`` verified by ``actor`` and return it.

    Raises ``NotAllowedToVerify`` for an actor who may not. Does nothing
    and records nothing when the Organizer is already verified.
    """
    return _set_verified(organizer, actor=actor, reason=reason, verified=True)


def unverify_organizer(
    organizer: Organizer, *, actor: "_User", reason: str = ""
) -> Organizer:
    """Remove ``organizer``'s verification and return it.

    Raises ``NotAllowedToVerify`` for an actor who may not. A change also
    closes every open Organizer Report on it in the same transaction, one
    ``organizer_report.close`` Audit Event each, with ``actor`` and
    ``reason``. Does nothing and records nothing when the Organizer is not
    verified.
    """
    return _set_verified(organizer, actor=actor, reason=reason, verified=False)


def _set_verified(
    organizer: Organizer, *, actor: "_User", reason: str, verified: bool
) -> Organizer:
    organizer.verification_changed = False
    if not (
        actor.is_authenticated and actor.is_active and actor.has_perm(VERIFY_PERMISSION)
    ):
        raise NotAllowedToVerify(
            "Only an active Account with permission can verify an organizer."
        )
    with transaction.atomic():
        # The lock makes concurrent calls run in turn, so at most one of
        # them changes the row and records an event.
        locked = Organizer.objects.select_for_update().get(pk=organizer.pk)
        if OrganizerMember.objects.filter(organizer=locked, account=actor).exists():
            raise NotAllowedToVerify(
                "A member of an organizer cannot verify or unverify it."
            )
        changed = locked.is_verified != verified
        if changed:
            before = snapshot(locked, _FIELDS)
            if verified:
                locked.verified_at = timezone.now()
                locked.verified_by = actor
            else:
                locked.verified_at = None
                locked.verified_by = None
            locked.save(update_fields=_FIELDS)
            record_event(
                action=(
                    AuditAction.ORGANIZER_VERIFY
                    if verified
                    else AuditAction.ORGANIZER_UNVERIFY
                ),
                target=locked,
                actor=actor,
                before=before,
                after=snapshot(locked, _FIELDS),
                reason=reason,
            )
            if verified:
                # Under the Organizer lock, so a request made meanwhile
                # waits and then sees the Organizer verified (409).
                close_open_verification_requests(locked, actor=actor, reason=reason)
            else:
                # In the same transaction, under the Organizer lock, so a
                # report filed meanwhile waits and then sees it unverified.
                close_open_reports(
                    locked,
                    actor=actor,
                    reason=reason,
                    rule=RULE_ORGANIZER_UNVERIFIED,
                )
    organizer.verified_at = locked.verified_at
    organizer.verification_changed = changed
    organizer.verified_by_id = locked.verified_by_id
    return organizer

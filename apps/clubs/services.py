"""Verify and unverify Organizers.

A verified Organizer's submissions count as confirmed (CLAUDE.md, hard
rule 8), so these are the only ways to change ``verified_at`` and
``verified_by``. Each refuses, before any write, an actor that is not an
active Account with ``clubs.verify_organizer``, or that is a member of the
Organizer. A change records one Audit Event in the same transaction. An
unverify that changes the status also closes every open Organizer Report
on it (``apps.clubs.reports``) in that transaction, with one
``organizer_report.close`` Audit Event each. A call that would change
nothing records nothing. Each call sets
``organizer.verification_changed`` to whether that call changed the
status, so a caller (the admin) reports only its own changes.
"""

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot
from apps.clubs.models import VERIFY_PERMISSION, Organizer, OrganizerMember
from apps.clubs.reports import RULE_ORGANIZER_UNVERIFIED, close_open_reports

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

_FIELDS = ["verified_at", "verified_by"]


class NotAllowedToVerify(PermissionError):
    """The actor may not verify or unverify this Organizer."""


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
            if not verified:
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

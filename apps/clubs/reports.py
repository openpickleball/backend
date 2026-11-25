"""Reports of verified Organizers to OPEN Moderators (#271).

Any active Account with a Player may report a verified Organizer with one
of the fixed reasons (``OrganizerReportReason``), once while that report
is open. A moderator (an active Account with ``clubs.verify_organizer``
that is not a member of the Organizer) closes a report with a reason, and
``unverify_organizer`` closes every open report on the Organizer it
unverifies. Each create and close records one Audit Event in the same
transaction. A report changes no Match, flag, rating, or Organizer field.

The reporter is stored on the report row and as the Audit Event's actor,
and nowhere else: never in an API response, an email, or anything an
Organizer member can reach.

Lock order: ``report_organizer`` and ``unverify_organizer`` lock the
Organizer row first, so a report never lands open after an unverify
closed the others. ``close_report`` locks only the report row, never the
Organizer, so it cannot form a cycle with them.
"""

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot
from apps.clubs.models import (
    Organizer,
    OrganizerMember,
    OrganizerReport,
    OrganizerReportReason,
)
from apps.players.models import Player

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

# The permission a moderator needs to close a report: the same one that
# verifies and unverifies Organizers.
CLOSE_PERMISSION = "clubs.verify_organizer"
OPEN_REPORT_CONSTRAINT = "clubs_one_open_report_per_account"
RULE_MODERATOR = "moderator"
RULE_ORGANIZER_UNVERIFIED = "organizer_unverified"

# Fixed texts, never a submitted value.
_REFUSED = "This organizer cannot be reported."
_EXISTS = "You already have an open report on this organizer."
_UNKNOWN_REASON = "Choose one of the listed reasons."
_NOT_ALLOWED = "Only a moderator who is not a member of the organizer may close it."
_REASON_REQUIRED = "Give a reason to close the report."
_CLOSED_FIELDS = ["closed_at"]


class ReportRefused(PermissionError):
    """The actor may not report this Organizer, or it is not verified.

    One fixed message for every cause, so a caller cannot tell an unknown
    Organizer from an unverified one or a refused actor.
    """

    def __init__(self) -> None:
        super().__init__(_REFUSED)


class ReportExists(ValueError):
    """The actor already has an open report on this Organizer."""

    def __init__(self) -> None:
        super().__init__(_EXISTS)


class NotAllowedToClose(PermissionError):
    """The actor may not close this report."""

    def __init__(self) -> None:
        super().__init__(_NOT_ALLOWED)


def report_organizer(
    organizer_id: int, *, actor: "_User", reason: str
) -> OrganizerReport:
    """Store ``actor``'s report of the Organizer with this row id.

    Raises ``ValueError`` for a reason that is not an
    ``OrganizerReportReason``, ``ReportRefused`` when the actor is not an
    active Account with a Player (read from the database) or the Organizer
    is missing or not verified, and ``ReportExists`` when the actor
    already has an open report on it. Records one
    ``organizer_report.create`` Audit Event.
    """
    if not isinstance(reason, str) or reason not in OrganizerReportReason.values:
        raise ValueError(_UNKNOWN_REASON)
    if actor.pk is None or not (
        Player.objects.filter(account_id=actor.pk, account__is_active=True).exists()
    ):
        raise ReportRefused
    try:
        with transaction.atomic():
            # FOR NO KEY UPDATE: waits for an unverify (FOR UPDATE) to
            # commit, and is waited on by one, without blocking inserts
            # that point at the Organizer.
            organizer = (
                Organizer.objects.select_for_update(no_key=True)
                .filter(pk=organizer_id)
                .first()
            )
            if organizer is None or not organizer.is_verified:
                raise ReportRefused
            # A concurrent duplicate from the same Account waits on the
            # Organizer lock, then fails the partial unique constraint.
            with transaction.atomic():
                report = OrganizerReport.objects.create(
                    organizer=organizer, reporter_id=actor.pk, reason=reason
                )
            record_event(
                action=AuditAction.ORGANIZER_REPORT_CREATE,
                target=report,
                actor=actor,
                # The Organizer's OPEN Code, never its name (ADR 0009).
                after={"reason": reason, "organizer": organizer.public_code},
            )
    except IntegrityError as error:
        if _constraint_name(error) == OPEN_REPORT_CONSTRAINT:
            raise ReportExists from None
        raise
    return report


def _constraint_name(error: IntegrityError) -> str | None:
    diag = getattr(error.__cause__, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def may_close(actor: "_User", organizer_id: int) -> bool:
    """True when ``actor`` may close a report on this Organizer."""
    return bool(
        actor.is_authenticated
        and actor.is_active
        and actor.has_perm(CLOSE_PERMISSION)
        and not OrganizerMember.objects.filter(
            organizer_id=organizer_id, account_id=actor.pk
        ).exists()
    )


def close_report(report: OrganizerReport, *, actor: "_User", reason: str) -> bool:
    """Close ``report`` as a moderator, with a required ``reason``.

    Raises ``NotAllowedToClose`` for an actor who is not an active Account
    with ``clubs.verify_organizer``, or who is a member of the report's
    Organizer, and ``ValueError`` for a blank reason. Returns False and
    records nothing when the report is already closed. Otherwise records
    one ``organizer_report.close`` Audit Event with rule ``moderator``.
    """
    if not may_close(actor, report.organizer_id):
        raise NotAllowedToClose
    if not reason.strip():
        raise ValueError(_REASON_REQUIRED)
    with transaction.atomic():
        locked = OrganizerReport.objects.select_for_update().get(pk=report.pk)
        if locked.closed_at is not None:
            return False
        _close(locked, actor=actor, reason=reason, rule=RULE_MODERATOR)
    report.closed_at = locked.closed_at
    return True


def close_open_reports(
    organizer: Organizer, *, actor: "_User", reason: str, rule: str
) -> int:
    """Close every open report on ``organizer`` and return how many.

    For ``apps.clubs.services``: call inside the transaction that holds
    the Organizer row's lock. One ``organizer_report.close`` Audit Event
    each, naming ``rule``.
    """
    reports = OrganizerReport.objects.select_for_update().filter(
        organizer_id=organizer.pk, closed_at__isnull=True
    )
    count = 0
    for report in reports.order_by("pk"):
        _close(report, actor=actor, reason=reason, rule=rule)
        count += 1
    return count


def _close(locked: OrganizerReport, *, actor: "_User", reason: str, rule: str) -> None:
    before = snapshot(locked, _CLOSED_FIELDS)
    locked.closed_at = timezone.now()
    locked.save(update_fields=_CLOSED_FIELDS)
    record_event(
        action=AuditAction.ORGANIZER_REPORT_CLOSE,
        target=locked,
        actor=actor,
        before=before,
        after={**snapshot(locked, _CLOSED_FIELDS), "rule": rule},
        reason=reason,
    )

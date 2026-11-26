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
Organizer, so it cannot form a cycle with them, and so does
``delete_reporter_reports`` (Account deletion, #116) for the reporter's
rows.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import CharField, Count, Q
from django.db.models.functions import Cast
from django.utils import timezone

from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event, snapshot
from apps.clubs.models import (
    VERIFY_PERMISSION,
    Organizer,
    OrganizerMember,
    OrganizerReport,
    OrganizerReportReason,
)
from apps.matches.models import SIGNAL_FLAG_REASONS, Match, MatchFlag, MatchStatus
from apps.players.models import Player

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser, _User

OPEN_REPORT_CONSTRAINT = "clubs_one_open_report_per_account"
RULE_MODERATOR = "moderator"
RULE_ORGANIZER_UNVERIFIED = "organizer_unverified"
RULE_REPORTER_DELETED = "reporter_deleted"

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


def may_close(actor: "_User | AnonymousUser", organizer_id: int) -> bool:
    """True when ``actor`` may close a report on this Organizer."""
    return bool(
        actor.is_authenticated
        and actor.is_active
        and actor.has_perm(VERIFY_PERMISSION)
        and not OrganizerMember.objects.filter(
            organizer_id=organizer_id, account_id=actor.pk
        ).exists()
    )


def close_report(
    report: OrganizerReport, *, actor: "_User | AnonymousUser", reason: str
) -> bool:
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


def delete_reporter_reports(
    reporter: "_User", *, actor: "_User | AnonymousUser | None"
) -> int:
    """Delete every report ``reporter`` filed, for Account deletion (#116).

    Call inside the deletion's transaction. Each open report is closed
    first, with one ``organizer_report.close`` Audit Event naming rule
    ``reporter_deleted``, so a moderator's queue never loses a report
    without a trace. Then every report row of the reporter goes; the
    ``organizer_report.create`` events stay (append-only, ADR 0037).
    Locks only the report rows, as ``close_report`` does. Returns how
    many rows it deleted.
    """
    reports = list(
        OrganizerReport.objects.select_for_update()
        .filter(reporter_id=reporter.pk)
        .order_by("pk")
    )
    for report in reports:
        if report.closed_at is None:
            _close(report, actor=actor, reason="", rule=RULE_REPORTER_DELETED)
    deleted, _ = OrganizerReport.objects.filter(
        pk__in=[report.pk for report in reports]
    ).delete()
    return int(deleted)


def _close(
    locked: OrganizerReport,
    *,
    actor: "_User | AnonymousUser | None",
    reason: str,
    rule: str,
) -> None:
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


@dataclass(frozen=True)
class OrganizerReview:
    """What a moderator sees about one Organizer (#271).

    The open reports, oldest first, and counts over the ``window_days``
    days since ``since`` of what happened to disputes on the matches of
    the Organizer's Events, read from their Audit Events: disputes opened
    (``match.dispute``), withdrawn (``match.dispute_withdraw``), resolved
    as confirmed or voided (``dispute.outcome``), and expired
    (``match.expire`` by the dispute rule), and the flags on those
    matches whose reason is a signal (``SIGNAL_FLAG_REASONS``).
    """

    window_days: int
    since: datetime
    open_reports: list[OrganizerReport]
    disputes_opened: int
    disputes_withdrawn: int
    disputes_confirmed: int
    disputes_voided: int
    disputes_expired: int
    signal_flags: int


def organizer_review(
    organizer: Organizer, *, now: datetime | None = None
) -> OrganizerReview:
    """The moderator review of ``organizer``, as of ``now`` (default: now).

    Only matches of Events whose Organizer is ``organizer`` count; a
    self-reported match or another Organizer's never does. Reads only:
    it locks and writes nothing.
    """
    window_days: int = settings.ORGANIZER_REVIEW_WINDOW_DAYS
    now = now or timezone.now()
    since = now - timedelta(days=window_days)
    matches = Match.objects.filter(event__organizer_id=organizer.pk)
    # target_object_id is text, so compare with the Matches' ids as text.
    match_ids = matches.annotate(text_id=Cast("pk", output_field=CharField())).values(
        "text_id"
    )
    counts = AuditEvent.objects.filter(
        target_content_type=ContentType.objects.get_for_model(Match),
        target_object_id__in=match_ids,
        created_at__gte=since,
        created_at__lte=now,
    ).aggregate(
        opened=Count("pk", filter=Q(action=AuditAction.MATCH_DISPUTE)),
        withdrawn=Count("pk", filter=Q(action=AuditAction.MATCH_DISPUTE_WITHDRAW)),
        confirmed=Count(
            "pk",
            filter=Q(
                action=AuditAction.DISPUTE_OUTCOME,
                after__status=MatchStatus.CONFIRMED,
            ),
        ),
        voided=Count(
            "pk",
            filter=Q(
                action=AuditAction.DISPUTE_OUTCOME,
                after__status=MatchStatus.VOIDED,
            ),
        ),
        expired=Count(
            "pk",
            filter=Q(action=AuditAction.MATCH_EXPIRE, after__rule="dispute"),
        ),
    )
    signal_flags = MatchFlag.objects.filter(
        match__in=matches,
        reason__in=SIGNAL_FLAG_REASONS,
        created_at__gte=since,
        created_at__lte=now,
    ).count()
    open_reports = list(
        OrganizerReport.objects.filter(
            organizer_id=organizer.pk, closed_at__isnull=True
        ).order_by("created_at", "pk")
    )
    return OrganizerReview(
        window_days=window_days,
        since=since,
        open_reports=open_reports,
        disputes_opened=counts["opened"],
        disputes_withdrawn=counts["withdrawn"],
        disputes_confirmed=counts["confirmed"],
        disputes_voided=counts["voided"],
        disputes_expired=counts["expired"],
        signal_flags=signal_flags,
    )

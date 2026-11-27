"""Unusual submission rules: signal flags on trusted submitters' matches (#206).

A match with an Event comes from a trusted submitter (its verified
Organizer's member, or an approved application acting for it, ADR 0021)
and counts at once. ``record_match()`` calls ``flag_unusual_submission``
on each new one, last in its write transaction, under the submitter's
advisory lock, and it writes one ``unusual_submission`` flag per rule
that fires (ADR 0049):

- ``first_matches``: the match is among the first
  ``UNUSUAL_SUBMISSION_FIRST_MATCHES`` on its Organizer's Events created
  since the Organizer's current ``verified_at`` (so a new verification
  restarts the count), or, for an application, among its first that many
  since its ``approved_at``. One flag when both fire.
- ``large_batch``: the match's Submission Batch expects more than
  ``UNUSUAL_SUBMISSION_BATCH_SIZE`` matches (its stored
  ``expected_size``), so every match of a large upload is flagged from
  the first row.
- ``daily_volume``: the submitter (Account or application) has recorded
  more than ``UNUSUAL_SUBMISSION_DAILY_MATCHES`` matches on the new
  match's UTC ``created_at`` day; the match past that number and every
  later one that day are flagged.
- ``player_daily_matches`` (#207, ADR 0052): one of the match's players
  has more than ``UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES`` matches on the
  match's ``played_on``, the new match included. One flag, however many
  players are past the number.

Except for ``player_daily_matches``, every count includes only new
trusted submissions: matches with an Event
that correct nothing (``_new_submissions``). An ``edit_match()``
correction keeps its original's submitter and Event with a new
``created_at``, but it is not a new submission, so it never uses up a
first count or raises a daily number; a player's self-submission (no
Event) is not a trusted submission. Matches in any status count, voided
ones too, and the new match itself. ``player_daily_matches`` counts
every match a player played that day that is not voided and corrects
nothing, in any other status, self-reported matches included, so a
player's own submissions count toward it. It takes no per-player lock:
the submitter lock does not make it exact, and two concurrent
submissions of one player's matches may each miss the other, which errs
toward no flag (ADR 0052). Values are read from the database by id, and
thresholds from settings at call time.

A flag is a signal only (``SIGNAL_FLAG_REASONS``): it never holds a match
from counting (``Match.objects.counted()`` ignores it), changes no
rating or status, sends no signal or notice, writes no Audit Event, and
no API response shows it. The submitter is never refused or limited.
Moderators see these flags only as counts in the Organizer review
(``apps.clubs.reports.organizer_review``, ADR 0037).

Nothing here reads payment status (CLAUDE.md, hard rule 7).
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from django.conf import settings
from django.db.models import QuerySet
from oauth2_provider.models import get_application_model

from apps.events.models import Event
from apps.matches.models import (
    FlagReason,
    FlagRule,
    Match,
    MatchFlag,
    MatchParticipant,
    MatchStatus,
    SubmissionBatch,
)

if TYPE_CHECKING:
    from apps.partners.models import PartnerApplication


def _new_submissions() -> QuerySet[Match]:
    """Matches recorded as new trusted submissions: with an Event, and not
    a correction. Any status, voided included."""
    return Match.objects.filter(event__isnull=False, corrects__isnull=True)


def _within_first(
    rows: QuerySet[Match] | QuerySet[MatchParticipant], number: int
) -> bool:
    """True when ``rows``, the new match among them, are at most ``number``.

    Reads at most ``number + 1`` rows.
    """
    return rows.values("pk")[: number + 1].count() <= number


def _first_matches(match: Match) -> bool:
    number = settings.UNUSUAL_SUBMISSION_FIRST_MATCHES
    if match.event_id is None:
        return False
    organizer = (
        Event.objects.filter(pk=match.event_id)
        .values("organizer_id", "organizer__verified_at")
        .first()
    )
    if organizer is not None and organizer["organizer__verified_at"] is not None:
        since: datetime = organizer["organizer__verified_at"]
        if _within_first(
            _new_submissions().filter(
                event__organizer_id=organizer["organizer_id"], created_at__gte=since
            ),
            number,
        ):
            return True
    if match.submitted_by_application_id is None:
        return False
    # The application model is PartnerApplication (CLAUDE.md ordering rules).
    applications = cast("type[PartnerApplication]", get_application_model())
    approved_at = (
        applications.objects.filter(pk=match.submitted_by_application_id)
        .values_list("approved_at", flat=True)
        .first()
    )
    if approved_at is None:
        return False
    return _within_first(
        _new_submissions().filter(
            submitted_by_application_id=match.submitted_by_application_id,
            created_at__gte=approved_at,
        ),
        number,
    )


def _large_batch(match: Match) -> bool:
    if match.submission_batch_id is None:
        return False
    expected = (
        SubmissionBatch.objects.filter(pk=match.submission_batch_id)
        .values_list("expected_size", flat=True)
        .first()
    )
    return expected is not None and expected > settings.UNUSUAL_SUBMISSION_BATCH_SIZE


def _daily_volume(match: Match) -> bool:
    number = settings.UNUSUAL_SUBMISSION_DAILY_MATCHES
    day = match.created_at.astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if match.submitted_by_user_id is not None:
        submitted = _new_submissions().filter(
            submitted_by_user_id=match.submitted_by_user_id
        )
    elif match.submitted_by_application_id is not None:
        submitted = _new_submissions().filter(
            submitted_by_application_id=match.submitted_by_application_id
        )
    else:
        return False
    return not _within_first(
        submitted.filter(created_at__gte=day, created_at__lt=day + timedelta(days=1)),
        number,
    )


def _player_daily_matches(match: Match) -> bool:
    number = settings.UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES
    player_ids = MatchParticipant.objects.filter(match_id=match.pk).values_list(
        "player_id", flat=True
    )
    for player_id in player_ids:
        played = MatchParticipant.objects.filter(
            player_id=player_id,
            match__played_on=match.played_on,
            match__corrects__isnull=True,
        ).exclude(match__status=MatchStatus.VOIDED)
        if not _within_first(played, number):
            return True
    return False


def unusual_rules(match: Match) -> list[FlagRule]:
    """The rules that fire for ``match``, a new stored match with an Event."""
    rules: list[FlagRule] = []
    if _first_matches(match):
        rules.append(FlagRule.FIRST_MATCHES)
    if _large_batch(match):
        rules.append(FlagRule.LARGE_BATCH)
    if _daily_volume(match):
        rules.append(FlagRule.DAILY_VOLUME)
    if _player_daily_matches(match):
        rules.append(FlagRule.PLAYER_DAILY_MATCHES)
    return rules


def flag_unusual_submission(match: Match) -> list[MatchFlag]:
    """Write one ``unusual_submission`` flag per rule that fires for ``match``.

    Call it only from ``record_match()``, for a new match with an Event,
    last in its write transaction and under the submitter's advisory lock
    (``apps.matches.services.submitter_lock_key``), so one submitter's
    concurrent submissions count each other. Returns the flags written.
    """
    if match.event_id is None:
        return []
    return MatchFlag.objects.bulk_create(
        [
            MatchFlag(match=match, reason=FlagReason.UNUSUAL_SUBMISSION, rule=rule)
            for rule in unusual_rules(match)
        ]
    )

"""The match confirmation workflow: every change to a Match after it is created.

Score confirmation is a security-sensitive area (CLAUDE.md, hard rule 8),
and the ledger is append-only (hard rule 3). These services are the only
code that changes a Match's status, and nothing changes any other field
of a Match: an edit voids the old match and creates a corrected one.

States and transitions:

- ``confirm_match``: a participant records their confirmation. A pending
  match becomes confirmed once at least one player of each team has
  confirmed it. A member (either role) of the match's Event's verified
  Organizer, or an approved application that Organizer owns, confirms a
  pending match alone.
- ``dispute_match``: a participant disputes a pending or confirmed match,
  which becomes disputed.
- ``resolve_dispute``: a moderator resolves a disputed match as confirmed
  or voided, with a reason.
- ``void_match``: a moderator, or an actor ``can_change_match`` allows,
  voids a match in any other status.
- ``edit_match``: the same actors void a match and create its correction
  (``corrects`` links it to the old one) in one transaction, through the
  creation core ``record_match`` uses: validation, model rules, locks,
  duplicate checks, and flags. Only a moderator edits a disputed match.
- ``expire_unconfirmed_matches``: with ``MATCH_CONFIRMATION_EXPIRY_DAYS``
  set, voids pending matches older than that.
- ``resolve_flags``: a moderator closes every open flag that involves a
  match, as approved or dismissed, with a reason. It changes no Match:
  a resolved flag stops holding its matches from counting.
- ``approve_match``: a moderator says the result stands: a disputed match
  is resolved as confirmed and its open flags as approved, in one
  transaction, with the status read under the match's locks.
- ``void_submission_batch``: a moderator voids every match of one
  Submission Batch (an upload), found by the batch link, each through
  ``void_match`` in its own transaction, with one ``match.bulk_void``
  Audit Event for the run.
- ``void_submitter_matches``: the same for every match one submitter (an
  Account or a Partner Application) recorded on or after a date.

"Moderator" here means ``apps.matches.permissions.moderates``: a
moderator whose own Player played in no match of the match's correction
chain. A moderator who played is refused on the moderator-only paths and
falls back to the other rules (``can_change_match``) for void and edit,
which also refuse their reason. An edit as moderator that puts the
moderator's own Player in the correction falls back the same way.

Each service checks permission first and raises ``MatchActionDenied``
(fixed text) before it reveals anything about the match. It then locks
the match row and checks its state, raising ``MatchStateError`` (fixed
text) for a change the state does not allow. Every transition writes one
Audit Event in its transaction; a repeated action that changes nothing
writes none. The exception is the bulk services (``void_submission_batch``,
``void_submitter_matches``), which record one ``match.bulk_void`` every
run, even a rerun that voids nothing. They void each match in its own
transaction, so never call them inside an outer ``transaction.atomic()``:
that would hold every match's lineup lock until it ends. Signals (``apps.matches.signals``) are sent inside the
transaction, after the rows and the Audit Event.

Lock order: every service takes advisory locks before the row lock, as
``record_match`` does, so no two paths wait on each other in opposite
orders. Each takes the match's lineup lock (``record_match``'s key for its
players and date), so whether flag-linked matches count, which decides
hold and release signals, is read and changed one transaction at a time.
``edit_match`` also takes the external id lock and the new lineup's lock.
Expiry only tries the lineup lock and skips a match it cannot get.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone
from oauth2_provider.models import get_application_model

from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot
from apps.matches.models import (
    ConfirmationDecision,
    FlagReason,
    FlagResolution,
    Match,
    MatchConfirmation,
    MatchFlag,
    MatchParticipant,
    MatchStatus,
    SubmissionBatch,
    Team,
)
from apps.matches.permissions import (
    Actor,
    MatchActionDenied,
    MatchSubmission,
    can_change_match,
    can_confirm_as_organizer,
    can_create_match,
    is_moderator,
    moderates,
    participant_player,
)
from apps.matches.services import (
    advisory_lock,
    audit_actor,
    check_external_id,
    draft_match,
    duplicate_error,
    exact_duplicate,
    insert_draft,
    lock_draft,
    near_duplicates,
    stored_lineup_lock_key,
    try_advisory_lock,
)
from apps.matches.signals import MatchChange, match_changed, match_confirmed
from apps.players.models import Player

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

    from apps.partners.models import PartnerApplication

logger = logging.getLogger(__name__)

_RESOLUTIONS = frozenset({MatchStatus.CONFIRMED, MatchStatus.VOIDED})


class MatchStateError(ValueError):
    """The match's status does not allow this change. Fixed text."""

    message = "This match cannot make this change in its current status."

    def __init__(self) -> None:
        super().__init__(self.message)


class ExpiryFailed(RuntimeError):
    """Some matches could not be expired. ``failed`` is how many.

    The message gives only the count.
    """

    def __init__(self, failed: int) -> None:
        self.failed = failed
        super().__init__(f"{failed} unconfirmed matches could not be expired.")


def _lock_row(match: Match) -> Match:
    """The match's row, locked until the transaction ends."""
    return Match.objects.select_for_update().get(pk=match.pk)


def _lock(match: Match) -> Match:
    """Take the match's lineup lock, then its row lock, and return the row.

    The lineup lock is the one ``record_match`` takes for this lineup and
    date, so whether flag-linked matches count (they share it) is read and
    changed by one transaction at a time. A match's lineup never changes,
    so the key can be read before the lock.
    """
    advisory_lock(stored_lineup_lock_key(match))
    return _lock_row(match)


def _linked(match: Match) -> list[Match]:
    """Other matches an open possible duplicate flag links to ``match``.

    Each Q names one flag row, so the reason and the open check apply to
    the same flag. A resolved flag holds nothing (``counted()``).
    """
    return list(
        Match.objects.filter(
            Q(
                flags__duplicate_of=match,
                flags__reason=FlagReason.POSSIBLE_DUPLICATE,
                flags__resolved_at__isnull=True,
            )
            | Q(
                duplicate_flags__match=match,
                duplicate_flags__reason=FlagReason.POSSIBLE_DUPLICATE,
                duplicate_flags__resolved_at__isnull=True,
            )
        )
        .exclude(pk=match.pk)
        .distinct()
        .order_by("pk")
    )


def _counting(matches: Iterable[Match]) -> dict[int, tuple[Match, bool]]:
    return {match.pk: (match, match.is_counted()) for match in matches}


def _send_count_changes(before: dict[int, tuple[Match, bool]]) -> None:
    """Send hold or release for each match whose counting changed."""
    for match, counted_before in before.values():
        counted_now = match.is_counted()
        if counted_before == counted_now:
            continue
        match_changed.send(
            sender=Match,
            match=match,
            change=MatchChange.HOLD if counted_before else MatchChange.RELEASE,
            earliest_date=match.played_on,
        )


def _set_status(match: Match, status: MatchStatus) -> dict[str, object]:
    """Save a new status on a locked match. Returns the snapshot before."""
    before = snapshot(match, ["status"])
    match.status = status
    match.save(update_fields=["status"])
    return before


def _saved(match: Match) -> None:
    if match.pk is None:
        raise MatchActionDenied


def _both_teams_confirmed(match: Match) -> bool:
    confirmed_players = MatchConfirmation.objects.filter(
        match=match, decision=ConfirmationDecision.CONFIRM
    ).values("player_id")
    teams = set(
        MatchParticipant.objects.filter(
            match=match, player_id__in=confirmed_players
        ).values_list("team", flat=True)
    )
    return teams >= {Team.A, Team.B}


def awaiting_confirmation(player: Player) -> QuerySet[Match]:
    """The matches waiting for ``player``'s team to confirm them. Read only.

    A Match is listed when it is pending, ``player`` played in it, and no
    player on ``player``'s team has confirmed it. A pending match needs one
    confirmation from each team (``_both_teams_confirmed``), so once a
    teammate confirms, the team is done and the match is not listed. A
    match the other team alone confirmed is listed. ``record_match`` itself
    records no confirmation, so a match it created is listed for its
    submitter too, but ``create_match`` confirms for the submitter's team
    in the same transaction (#197), so a match submitted there is not.

    One row per Match: a Player plays in a Match at most once (a database
    constraint), so the join on participants adds no duplicates.
    """
    viewer_team = MatchParticipant.objects.filter(
        match=OuterRef("pk"), player=player
    ).values("team")[:1]
    team_confirmed = MatchConfirmation.objects.filter(
        match=OuterRef("pk"),
        decision=ConfirmationDecision.CONFIRM,
        player__match_participations__match=OuterRef("pk"),
        player__match_participations__team=OuterRef("viewer_team"),
    )
    return (
        Match.objects.filter(status=MatchStatus.PENDING, participants__player=player)
        .annotate(viewer_team=Subquery(viewer_team))
        .filter(~Exists(team_confirmed))
    )


def _record_decision(
    match: Match, player: Player, decision: ConfirmationDecision
) -> bool:
    """Add the player's decision, unless they already gave it.

    Returns True when it added the row, False when it was already there.
    """
    if MatchConfirmation.objects.filter(
        match=match, player=player, decision=decision
    ).exists():
        return False
    MatchConfirmation.objects.create(match=match, player=player, decision=decision)
    return True


def _confirm(locked: Match, actor: Actor) -> None:
    before = _set_status(locked, MatchStatus.CONFIRMED)
    record_event(
        action=AuditAction.MATCH_CONFIRM,
        target=locked,
        before=before,
        after=snapshot(locked, ["status"]),
        **audit_actor(actor),
    )
    match_confirmed.send(sender=Match, match=locked)


def confirm_match(match: Match, *, actor: Actor) -> Match:
    """Record ``actor``'s confirmation of ``match`` and return the match.

    An organizer (see ``can_confirm_as_organizer``) confirms a pending
    match alone; for a confirmed match it does nothing. A participant adds
    their confirmation to a pending or confirmed match, and a pending match
    becomes confirmed once each team has one. A repeat does nothing. A
    disputed or voided match raises ``MatchStateError``: disputes go to a
    moderator. Anyone else gets ``MatchActionDenied``. An actor who is
    both organizer and participant takes the organizer path.
    """
    _saved(match)
    organizer = can_confirm_as_organizer(actor, match)
    player = None if organizer else participant_player(actor, match)
    if not organizer and player is None:
        raise MatchActionDenied
    with transaction.atomic():
        locked = _lock(match)
        if organizer:
            if locked.status == MatchStatus.CONFIRMED:
                return locked
            if locked.status != MatchStatus.PENDING:
                raise MatchStateError
            _confirm(locked, actor)
            return locked
        assert player is not None
        if locked.status not in (MatchStatus.PENDING, MatchStatus.CONFIRMED):
            raise MatchStateError
        _record_decision(locked, player, ConfirmationDecision.CONFIRM)
        if locked.status == MatchStatus.PENDING and _both_teams_confirmed(locked):
            _confirm(locked, actor)
        return locked


def dispute_match(match: Match, *, actor: Actor) -> Match:
    """Record a participant's dispute of ``match`` and return the match.

    A pending or confirmed match becomes disputed, with one
    ``match.dispute`` Audit Event, and a match that was counting sends
    ``match_changed`` with ``change=dispute``. A disputed match only gains
    the participant's dispute row. A participant disputes a match at most
    once: a repeat (also after a moderator resolved the dispute as
    confirmed) does nothing. A voided match raises ``MatchStateError``.
    Only a participant may dispute. A dispute takes no reason: Audit
    Events are append-only, and a participant's free text could hold a
    name.
    """
    _saved(match)
    player = participant_player(actor, match)
    if player is None:
        raise MatchActionDenied
    with transaction.atomic():
        locked = _lock(match)
        if locked.status == MatchStatus.VOIDED:
            raise MatchStateError
        # A participant disputes a match once. A repeat, even after a
        # moderator resolved the dispute as confirmed, changes nothing.
        added = _record_decision(locked, player, ConfirmationDecision.DISPUTE)
        if not added or locked.status == MatchStatus.DISPUTED:
            return locked
        was_counting = locked.is_counted()
        before = _set_status(locked, MatchStatus.DISPUTED)
        record_event(
            action=AuditAction.MATCH_DISPUTE,
            target=locked,
            before=before,
            after=snapshot(locked, ["status"]),
            **audit_actor(actor),
        )
        if was_counting:
            match_changed.send(
                sender=Match,
                match=locked,
                change=MatchChange.DISPUTE,
                earliest_date=locked.played_on,
            )
        return locked


def resolve_dispute(match: Match, *, actor: Actor, outcome: str, reason: str) -> Match:
    """A moderator resolves a disputed match as confirmed or voided.

    ``reason`` is required. Records one ``dispute.outcome`` Audit Event. A
    confirmed outcome sends ``match_confirmed``; a voided one sends
    ``release`` for each match it no longer holds.
    """
    _saved(match)
    if not moderates(actor, match):
        raise MatchActionDenied
    if outcome not in _RESOLUTIONS:
        raise ValueError("Resolve a dispute as confirmed or voided.")
    if not reason.strip():
        raise ValueError("Give a reason for the dispute outcome.")
    with transaction.atomic():
        locked = _lock(match)
        if locked.status != MatchStatus.DISPUTED:
            raise MatchStateError
        linked = _counting(_linked(locked))
        before = _set_status(locked, MatchStatus(outcome))
        record_event(
            action=AuditAction.DISPUTE_OUTCOME,
            target=locked,
            before=before,
            after=snapshot(locked, ["status"]),
            reason=reason,
            **audit_actor(actor),
        )
        if locked.status == MatchStatus.CONFIRMED:
            match_confirmed.send(sender=Match, match=locked)
        _send_count_changes(linked)
        return locked


_REASON_REFUSED = "Only a moderator may give a reason."


class ReasonRefused(ValueError):
    """An actor not acting as moderator gave a reason. Fixed text."""

    def __init__(self) -> None:
        super().__init__(_REASON_REFUSED)


def _check_reason(moderator: bool, reason: str) -> None:
    """Refuse a non-moderator's reason with fixed text.

    Audit Events are append-only, so free text that could hold a name is
    stored only from moderators.
    """
    if not moderator and reason.strip():
        raise ReasonRefused


def _void(
    locked: Match, *, action: AuditAction, actor: Actor | None, reason: str
) -> None:
    """Void a locked match, audit it, and send its signals."""
    was_counting = locked.is_counted()
    linked = _counting(_linked(locked))
    before = _set_status(locked, MatchStatus.VOIDED)
    record_event(
        action=action,
        target=locked,
        before=before,
        after=snapshot(locked, ["status"]),
        reason=reason,
        **audit_actor(actor),
    )
    if was_counting:
        match_changed.send(
            sender=Match,
            match=locked,
            change=MatchChange.VOID,
            earliest_date=locked.played_on,
        )
    _send_count_changes(linked)


def void_match(match: Match, *, actor: Actor, reason: str = "") -> Match:
    """Void ``match``. For a moderator or an actor ``can_change_match`` allows.

    Any status but voided becomes voided, with one ``match.void`` Audit
    Event. Only a moderator may give a ``reason``; anyone else giving one
    gets ``ValueError``. A match that was counting sends ``match_changed`` with
    ``change=void``, and each match it no longer holds gets ``release``.
    """
    _saved(match)
    # A moderator who played in the match falls back to the other rules.
    moderator = moderates(actor, match)
    if not (moderator or can_change_match(actor, match)):
        raise MatchActionDenied
    _check_reason(moderator, reason)
    with transaction.atomic():
        locked = _lock(match)
        if locked.status == MatchStatus.VOIDED:
            raise MatchStateError
        _void(locked, action=AuditAction.MATCH_VOID, actor=actor, reason=reason)
        return locked


def edit_match(
    match: Match,
    *,
    actor: Actor,
    reason: str = "",
    format: str,
    team_a: Sequence[Player],
    team_b: Sequence[Player],
    games: Sequence[tuple[int, int]],
    played_on: date,
    best_of: int | None = None,
    game_target: int | None = None,
    scoring_type: str | None = None,
    played_at: datetime | None = None,
    played_at_utc_offset: int | None = None,
    location: str = "",
    event_label: str = "",
    bracket_label: str = "",
    sequence: int = 0,
) -> Match:
    """Void ``match`` and create its correction. Returns the new match.

    For a moderator, or an actor ``can_change_match`` allows that may also
    submit the correction (``can_create_match`` for the old match's Event
    and Club and the new players), checked before any validation; only a
    moderator edits a disputed match, and a voided one raises
    ``MatchStateError``. The correction keeps the old match's submitter,
    external id, Event, and Club, links to it through ``corrects``, and is
    confirmed when the old one was confirmed or disputed, else pending. It
    goes through validation, the model rules, and the duplicate checks
    (``MatchValidationError``, ``DuplicateMatchError``), and any error
    rolls everything back. One ``match.edit`` Audit Event on the old match
    records it (no separate ``match.void``). Only a moderator may give a
    ``reason``; anyone else giving one gets ``ValueError``. Signals: ``match_changed``
    with ``change=edit`` for an old match that was counting (the earlier
    of the two dates), else ``match_confirmed`` for a confirmed
    correction, and ``hold`` or ``release`` for each other match whose
    counting changed.
    """
    _saved(match)
    new_player_ids = [
        player.pk
        for player in (*team_a, *team_b)
        if isinstance(player, Player) and player.pk is not None
    ]
    # A moderator who played in the match, or who puts their own Player in
    # the correction, falls back to the other rules.
    moderator = moderates(actor, match, new_player_ids=new_player_ids)
    if not (moderator or can_change_match(actor, match)):
        raise MatchActionDenied
    stored = Match.objects.select_related(
        "submitted_by_user", "submitted_by_application", "event", "club"
    ).get(pk=match.pk)
    if not moderator:
        # An edit creates a match, so the editor also needs the authority
        # to submit it, checked before any validation.
        submission = MatchSubmission(
            event=stored.event,
            club=stored.club,
            player_ids=frozenset(
                player.pk
                for player in (*team_a, *team_b)
                if isinstance(player, Player) and player.pk is not None
            ),
        )
        if not can_create_match(actor, submission):
            raise MatchActionDenied
    _check_reason(moderator, reason)
    draft = draft_match(
        format=format,
        team_a=team_a,
        team_b=team_b,
        games=games,
        played_on=played_on,
        best_of=best_of,
        game_target=game_target,
        scoring_type=scoring_type,
        submitted_by_user=stored.submitted_by_user,
        submitted_by_application=stored.submitted_by_application,
        external_id=stored.external_id,
        event=stored.event,
        club=stored.club,
        played_at=played_at,
        played_at_utc_offset=played_at_utc_offset,
        location=location,
        event_label=event_label,
        bracket_label=bracket_label,
        sequence=sequence,
    )
    with transaction.atomic():
        lock_draft(draft, also=[stored_lineup_lock_key(stored)])
        locked = _lock_row(match)
        if locked.status == MatchStatus.VOIDED:
            raise MatchStateError
        if locked.status == MatchStatus.DISPUTED and not moderator:
            raise MatchStateError
        old_status = MatchStatus(locked.status)
        was_counting = locked.is_counted()
        # Only matches the held locks cover: those flagged with the old match
        # (its lineup) and near duplicates of the new lineup and date.
        others = _linked(locked) + [
            near for near in near_duplicates(draft) if near.pk != locked.pk
        ]
        counting = _counting(others)
        before = _set_status(locked, MatchStatus.VOIDED)

        check_external_id(draft, replaces=locked)
        existing = exact_duplicate(draft)
        if existing is not None:
            raise duplicate_error(existing, actor, reveal=moderator)
        new_status = (
            MatchStatus.PENDING
            if old_status == MatchStatus.PENDING
            else MatchStatus.CONFIRMED
        )
        corrected, _held = insert_draft(draft, status=new_status, corrects=locked)
        record_event(
            action=AuditAction.MATCH_EDIT,
            target=locked,
            before=before,
            after={
                **snapshot(locked, ["status"]),
                "corrected_by": corrected.public_code,
            },
            reason=reason,
            **audit_actor(actor),
        )
        if was_counting:
            match_changed.send(
                sender=Match,
                match=locked,
                change=MatchChange.EDIT,
                earliest_date=min(locked.played_on, corrected.played_on),
            )
        elif corrected.status == MatchStatus.CONFIRMED:
            match_confirmed.send(sender=Match, match=corrected)
        _send_count_changes(counting)
        return corrected


_FLAG_RESOLUTIONS = frozenset(FlagResolution.values)


def _moderates_with_linked(actor: Actor, match: Match) -> bool:
    """``moderates`` for ``match`` and every match an open flag links to it.

    Resolving a flag also releases the match at its other end, so a
    moderator who played in that match's correction chain may not resolve
    it either. The links are read from the database (``_linked``).
    """
    if not moderates(actor, match):
        return False
    return all(moderates(actor, other) for other in _linked(match))


def resolve_flags(match: Match, *, actor: Actor, outcome: str, reason: str) -> int:
    """A moderator resolves every open flag that involves ``match``.

    That is each open flag with ``match`` as its ``match`` or its
    ``duplicate_of``. ``outcome`` is ``approved`` or ``dismissed`` and
    ``reason`` is required (``ValueError``, fixed text). Anyone who does
    not moderate ``match`` and every match an open flag links to it
    (``_moderates_with_linked``) gets ``MatchActionDenied`` first. No
    Match changes. Records
    one ``match.flag_resolve`` Audit Event per flag, with the reason, and
    sends ``release`` for each match that counts again. Returns how many
    flags it resolved: 0, with no event or signal, when none were open.
    """
    _saved(match)
    if not _moderates_with_linked(actor, match):
        raise MatchActionDenied
    if outcome not in _FLAG_RESOLUTIONS:
        raise ValueError("Resolve a flag as approved or dismissed.")
    if not reason.strip():
        raise ValueError("Give a reason for the flag resolution.")
    with transaction.atomic():
        # Flags link only matches with the same lineup and date, so the
        # match's lineup lock covers every match whose counting changes.
        locked = _lock(match)
        flags = list(
            MatchFlag.objects.select_for_update()
            .filter(Q(match=locked) | Q(duplicate_of=locked), resolved_at__isnull=True)
            .order_by("pk")
        )
        if not flags:
            return 0
        counting = _counting([locked, *_linked(locked)])
        now = timezone.now()
        for flag in flags:
            before = snapshot(flag, ["resolution", "resolved_at"])
            flag.resolution = FlagResolution(outcome)
            flag.resolved_at = now
            flag.save(update_fields=["resolution", "resolved_at"])
            record_event(
                action=AuditAction.MATCH_FLAG_RESOLVE,
                target=flag,
                before=before,
                after=snapshot(flag, ["resolution", "resolved_at"]),
                reason=reason,
                **audit_actor(actor),
            )
        _send_count_changes(counting)
        return len(flags)


def approve_match(match: Match, *, actor: Actor, reason: str) -> bool:
    """A moderator approves ``match``: its result stands.

    Anyone who does not moderate ``match`` and every match an open flag
    links to it (``_moderates_with_linked``) gets ``MatchActionDenied``
    first, and a blank ``reason`` raises ``ValueError``. Under the match's lineup and row
    locks, a voided match raises ``MatchStateError``, a disputed one is
    resolved as confirmed (``resolve_dispute``), and every open flag that
    involves it is resolved as approved (``resolve_flags``), all in one
    transaction with each step's Audit Event and signals. Returns True
    when anything changed, False when there was nothing to approve.
    """
    _saved(match)
    if not _moderates_with_linked(actor, match):
        raise MatchActionDenied
    if not reason.strip():
        raise ValueError("Give a reason for the approval.")
    with transaction.atomic():
        # Read the status only under the locks, so a dispute that commits
        # first is seen and resolved. The services below take the same
        # locks again, which the transaction already holds.
        locked = _lock(match)
        if locked.status == MatchStatus.VOIDED:
            raise MatchStateError
        changed = False
        if locked.status == MatchStatus.DISPUTED:
            resolve_dispute(
                locked, actor=actor, outcome=MatchStatus.CONFIRMED, reason=reason
            )
            changed = True
        if resolve_flags(
            locked, actor=actor, outcome=FlagResolution.APPROVED, reason=reason
        ):
            changed = True
        return changed


def expire_unconfirmed_matches(now: datetime | None = None) -> int:
    """Void pending matches older than ``MATCH_CONFIRMATION_EXPIRY_DAYS``.

    Does nothing and returns 0 when the setting is None (the default).
    Each match is voided in its own transaction, with one ``match.expire``
    Audit Event and no actor, and sends ``release`` for each match it no
    longer holds. A match another transaction holds locked is skipped
    until the next run, and a match no longer pending is left alone, so
    running twice is safe. Returns how many matches were voided. A match
    that fails rolls back alone and is logged by id; the run goes on, then
    raises ``ExpiryFailed`` with how many failed. The task's soft time
    limit (``SoftTimeLimitExceeded``) is not a failed match: it stops the
    run at once, keeping the matches already expired.
    """
    days: int | None = settings.MATCH_CONFIRMATION_EXPIRY_DAYS
    if days is None:
        return 0
    cutoff = (now or timezone.now()) - timedelta(days=days)
    candidates = list(
        Match.objects.filter(status=MatchStatus.PENDING, created_at__lt=cutoff)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    expired = 0
    failed = 0
    for pk in candidates:
        try:
            if _expire_one(pk, cutoff):
                expired += 1
        except SoftTimeLimitExceeded:
            # Out of time: stop cleanly. Matches already expired stay
            # committed, and the rest wait for the next run.
            raise
        except Exception:
            # Its transaction has rolled back. Log the id only: the error's
            # text could hold anything. The other matches still expire.
            failed += 1
            logger.error("Could not expire match %s.", pk)
    if expired:
        logger.info("Expired %d unconfirmed matches.", expired)
    if failed:
        raise ExpiryFailed(failed)
    return expired


def _expire_one(pk: int, cutoff: datetime) -> bool:
    """Void one pending match in its own transaction. True if it was voided."""
    with transaction.atomic():
        # Never wait: a match whose lineup another transaction holds is
        # left for the next run.
        if not try_advisory_lock(stored_lineup_lock_key(Match(pk=pk))):
            return False
        locked = (
            Match.objects.select_for_update(skip_locked=True)
            .filter(pk=pk, status=MatchStatus.PENDING, created_at__lt=cutoff)
            .first()
        )
        if locked is None:
            return False
        _void(locked, action=AuditAction.MATCH_EXPIRE, actor=None, reason="")
        return True


_BULK_REASON = "Give a reason for the bulk void."
_UNSAVED_BATCH = "Save the submission batch before voiding it."
_UNKNOWN_SUBMITTER = "The submitter must be a saved account or partner application."


@dataclass(frozen=True)
class BulkVoidResult:
    """What a bulk void did, as counts of matches.

    ``voided``: voided by this run. ``skipped``: already voided, including
    one voided by someone else while the run went on. ``refused``: the
    actor may not void it as moderator (``void_match`` raised
    ``MatchActionDenied`` or ``ReasonRefused``), which happens when the
    moderator's own Player is in the match's correction chain. It also
    counts every remaining match when the actor stops being a moderator
    partway through a run (``void_match`` checks again for each match).
    """

    voided: int
    skipped: int
    refused: int


def submission_batch_matches(batch: SubmissionBatch) -> QuerySet[Match]:
    """Every match of ``batch``, by the batch link, in row order. Read only."""
    return Match.objects.filter(submission_batch_id=batch.pk).order_by("pk")


def submitter_matches(
    submitter: "_User | PartnerApplication", recorded_since: date
) -> QuerySet[Match]:
    """Every match ``submitter`` recorded on or after ``recorded_since``.

    ``submitter`` is an Account or a Partner Application. The date is
    the recording time (``created_at``) in UTC, not ``played_on``, which
    the submitter controls. In row order. Read only. Anything else raises
    ``ValueError`` (fixed text).
    """
    start = datetime.combine(recorded_since, time.min, tzinfo=UTC)
    matches = Match.objects.filter(created_at__gte=start).order_by("pk")
    if isinstance(submitter, get_user_model()):
        return matches.filter(submitted_by_user_id=submitter.pk)
    if isinstance(submitter, get_application_model()):
        return matches.filter(submitted_by_application_id=submitter.pk)
    raise ValueError(_UNKNOWN_SUBMITTER)


def _check_bulk(actor: Actor, reason: str) -> None:
    """Refuse anyone but a moderator first, then a blank reason."""
    if not is_moderator(actor):
        raise MatchActionDenied
    if not reason.strip():
        raise ValueError(_BULK_REASON)


def void_submission_batch(
    batch: SubmissionBatch, *, actor: Actor, reason: str
) -> BulkVoidResult:
    """A moderator voids every match of ``batch``.

    Anyone but a moderator (``is_moderator``: an active Account with
    ``matches.moderate_match``) gets ``MatchActionDenied`` before anything
    is read or written. Then a blank ``reason`` and a batch with no stored
    row raise ``ValueError`` (fixed text). The matches are found by the batch link
    (``submission_batch_matches``), never by time. See ``_bulk_void`` for
    how each is voided and what is recorded.
    """
    _check_bulk(actor, reason)
    if batch.pk is None or not SubmissionBatch.objects.filter(pk=batch.pk).exists():
        raise ValueError(_UNSAVED_BATCH)
    return _bulk_void(
        submission_batch_matches(batch),
        actor=actor,
        reason=reason,
        target=batch,
        scope="batch",
        recorded_since=None,
    )


def void_submitter_matches(
    submitter: "_User | PartnerApplication",
    *,
    recorded_since: date,
    actor: Actor,
    reason: str,
) -> BulkVoidResult:
    """A moderator voids every match ``submitter`` recorded since a date.

    ``submitter`` is an Account or a Partner Application, and the scope
    is ``submitter_matches``: recorded (``created_at``, UTC) on or after
    ``recorded_since``. The same checks as ``void_submission_batch``, in
    the same order; a submitter with no stored row, or one that is
    neither an Account nor a Partner Application, raises ``ValueError``.
    """
    _check_bulk(actor, reason)
    if (
        not isinstance(submitter, (get_user_model(), get_application_model()))
        or submitter.pk is None
        or not type(submitter)._default_manager.filter(pk=submitter.pk).exists()
    ):
        raise ValueError(_UNKNOWN_SUBMITTER)
    return _bulk_void(
        submitter_matches(submitter, recorded_since),
        actor=actor,
        reason=reason,
        target=submitter,
        scope="submitter",
        recorded_since=recorded_since,
    )


def _bulk_void(
    matches: QuerySet[Match],
    *,
    actor: Actor,
    reason: str,
    target: "SubmissionBatch | _User | PartnerApplication",
    scope: str,
    recorded_since: date | None,
) -> BulkVoidResult:
    """Void each match through ``void_match``, then record the run.

    A match already voided in the list counts as skipped without a call.
    Each other match is voided in its own transaction (``void_match``),
    with its own ``match.void`` Audit Event and signals; this function
    opens no transaction around them, so no lineup lock is held past its
    own match. ``MatchStateError`` (voided meanwhile) counts as skipped,
    and ``MatchActionDenied`` or ``ReasonRefused`` as refused. Any other
    error propagates: the matches voided so far stay voided, and a rerun
    resumes. Then one ``match.bulk_void`` Audit Event, recorded every
    run, names the target (the batch or the submitter), the scope, the
    date, and the three counts, with the moderator's reason.
    """
    voided = skipped = refused = 0
    for match in list(matches):
        if match.status == MatchStatus.VOIDED:
            skipped += 1
            continue
        try:
            void_match(match, actor=actor, reason=reason)
        except MatchStateError:
            skipped += 1
        except (MatchActionDenied, ReasonRefused):
            refused += 1
        else:
            voided += 1
    with transaction.atomic():
        record_event(
            action=AuditAction.MATCH_BULK_VOID,
            target=target,
            after={
                "scope": scope,
                "recorded_since": (
                    recorded_since.isoformat() if recorded_since is not None else None
                ),
                "voided": voided,
                "skipped": skipped,
                "refused": refused,
            },
            reason=reason,
            **audit_actor(actor),
        )
    return BulkVoidResult(voided=voided, skipped=skipped, refused=refused)

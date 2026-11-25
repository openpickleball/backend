"""The match confirmation workflow: every change to a Match after it is created.

Score confirmation is a security-sensitive area (CLAUDE.md, hard rule 8),
and the ledger is append-only (hard rule 3). These services are the only
code that changes a Match's status, and nothing changes any other field
of a Match: an edit voids the old match and creates a corrected one.

States and transitions:

- ``confirm_match``: a participant records their confirmation. A pending
  match becomes confirmed once at least one player of each team has
  confirmed it. A member (either role) of the match's Event's verified
  Organizer, or an approved application that Organizer owns or granted an
  Organizer Application Grant, confirms a pending match alone.
- ``dispute_match``: a participant disputes a pending or confirmed match,
  which becomes disputed. After commit, the other team's players and, for
  an Event match, its verified Organizer's members get a dispute notice
  (``apps.matches.notices``, #272).
- ``withdraw_dispute``: a participant withdraws their open dispute (#267).
  Once no dispute is open, the match goes back to confirmed when it was
  confirmed as the dispute began, else to pending. A voided match refuses
  it.
- ``resolve_dispute``: the match's Event's verified Organizer
  (``settles_dispute``: a member whose Player is not in the match's
  correction chain, or the approved application that submitted the match
  and still acts for that Organizer, ADR 0034), or a moderator, resolves
  a disputed match as confirmed or voided, with a required reason.
- ``void_match``: a moderator, or an actor ``can_change_match`` allows,
  voids a match in any other status.
- ``edit_match``: the same actors void a match and create its correction
  (``corrects`` links it to the old one) in one transaction, through the
  creation core ``record_match`` uses: validation, model rules, locks,
  duplicate checks, and flags. Only a moderator, or the Event's verified
  Organizer settling the dispute with a required reason, edits a
  disputed match with an Event. On the player path (``as_player=True``,
  #267) a participant corrects a pending or disputed self-reported match
  (no Event, no Club) with the same teams; the correction is always
  pending, with the corrector's confirmation, and the corrector's Account
  is its submitter (the maintainer's decision on #267). Every other edit
  keeps the old match's submitter (ADR 0013).
- ``expire_unconfirmed_matches``: with ``MATCH_CONFIRMATION_EXPIRY_DAYS``
  set, voids pending matches older than that.
- ``expire_disputes_and_holds``: voids a disputed match whose first open
  dispute is older than ``MATCH_DISPUTE_EXPIRY_DAYS``, and the newer match
  of a possible duplicate flag open for longer than
  ``MATCH_DUPLICATE_HOLD_DAYS`` whose earlier match is confirmed. A
  correction keeps its original's place in that order while its chain
  keeps the same teams and date. That ends the flag's hold on the
  earlier match, which counts again unless another open flag still holds
  it (ADR 0034).
- ``vouch_for_match``: an actor says a match held by a possible duplicate
  flag is not a duplicate (ADR 0034, #270), at the match's own level
  (``vouch_level``): an Event match by its Event's verified Organizer, a
  self-reported match by one player of each team. Each vouch is a
  ``MatchVouch`` row with a ``match.vouch`` Audit Event. A flag whose two
  matches are both vouched for is resolved as approved, through the same
  flag row code as ``resolve_flags``. It changes no Match.
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
that would hold every match's lineup lock until it ends. Signals
(``apps.matches.signals``) are sent inside the transaction, after the rows
and the Audit Event.

Lock order: every service takes advisory locks before the row lock, as
``record_match`` does, so no two paths wait on each other in opposite
orders. Each takes the match's lineup lock (``record_match``'s key for its
players and date), so whether flag-linked matches count, which decides
hold and release signals, is read and changed one transaction at a time.
``edit_match`` also takes the external id lock and the new lineup's lock.
Expiry only tries the lineup lock and skips a match it cannot get.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, models, transaction
from django.db.models import CharField, Exists, OuterRef, Q, QuerySet, Subquery
from django.db.models.expressions import RawSQL
from django.db.models.functions import Cast
from django.utils import timezone
from oauth2_provider.models import get_application_model

from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event, snapshot
from apps.clubs.models import OrganizerMember
from apps.matches.models import (
    ConfirmationDecision,
    FlagReason,
    FlagResolution,
    Match,
    MatchConfirmation,
    MatchFlag,
    MatchParticipant,
    MatchStatus,
    MatchVouch,
    SubmissionBatch,
    Team,
)
from apps.matches.notices import schedule_dispute_notices
from apps.matches.permissions import (
    Actor,
    MatchActionDenied,
    MatchSubmission,
    can_change_match,
    can_confirm_as_organizer,
    can_correct_as_player,
    can_create_match,
    corrects_as_player,
    is_moderator,
    moderates,
    participant_player,
    settles_dispute,
    vouch_level,
)
from apps.matches.services import (
    advisory_lock,
    audit_actor,
    check_external_id,
    draft_match,
    duplicate_error,
    exact_duplicate,
    hold_event,
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
        super().__init__(f"{failed} matches could not be expired.")


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
    disputed or voided match raises ``MatchStateError``: a disputed match
    is settled by withdrawing every open dispute (``withdraw_dispute``), by
    the Event's Organizer, or by a moderator. Anyone else gets ``MatchActionDenied``. An actor who is
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
    once: a repeat (also after a moderator or the Event's Organizer resolved
    the dispute as confirmed, and after the participant withdrew it) does
    nothing. A voided match raises
    ``MatchStateError``. Only a participant may dispute. A dispute takes no
    reason: Audit Events are append-only, and a participant's free text
    could hold a name. A match that becomes disputed schedules its dispute
    notices (``schedule_dispute_notices``), queued after commit.
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
        # moderator or the Event's Organizer resolved the dispute as
        # confirmed, changes nothing.
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
        schedule_dispute_notices(
            locked,
            MatchParticipant.objects.filter(match=locked, player=player)
            .values_list("team", flat=True)
            .get(),
        )
        if was_counting:
            match_changed.send(
                sender=Match,
                match=locked,
                change=MatchChange.DISPUTE,
                earliest_date=locked.played_on,
            )
        return locked


@dataclass(frozen=True)
class _DisputePeriod:
    """The current disputed period of a match (``_dispute_period``)."""

    event: AuditEvent | None
    open_disputes: list[MatchConfirmation]
    opening_pk: int | None


def _dispute_period(match: Match) -> _DisputePeriod:
    """The current disputed period's ``match.dispute`` event and open disputes.

    The period starts at the match's latest ``match.dispute`` Audit Event
    (by id: every change into disputed writes exactly one). Its actor is
    the Account whose Player wrote the period's first dispute row, the
    opening row. The open disputes are the match's dispute rows from the
    opening row on, by id, whose player has not withdrawn, in id order.
    Rows before the opening row belong to an earlier period that a
    resolution settled, so they are never open again. With no event or no
    opening row (rows written outside the services), every dispute row not
    withdrawn counts as open and ``opening_pk`` is None. Both callers then
    fail closed: ``withdraw_dispute`` refuses every withdrawal, so the
    match stays disputed until a resolution or expiry, and
    ``dispute_started_at`` uses the event's time (the later, safe start).

    Only ids are compared, never times. One match's rows are written under
    its row lock, so id order is insertion order.
    """
    event = (
        _match_events()
        .filter(action=AuditAction.MATCH_DISPUTE, target_object_id=str(match.pk))
        .order_by("-pk")
        .first()
    )
    disputes = MatchConfirmation.objects.filter(
        match_id=match.pk, decision=ConfirmationDecision.DISPUTE
    )
    opening_pk: int | None = None
    if event is not None and event.actor_user_id is not None:
        opening_pk = (
            disputes.filter(player__account_id=event.actor_user_id)
            .values_list("pk", flat=True)
            .first()
        )
    if opening_pk is not None:
        disputes = disputes.filter(pk__gte=opening_pk)
    withdrawn = MatchConfirmation.objects.filter(
        match_id=match.pk, decision=ConfirmationDecision.WITHDRAW
    ).values("player_id")
    open_disputes = list(disputes.exclude(player_id__in=withdrawn).order_by("pk"))
    return _DisputePeriod(event, open_disputes, opening_pk)


def withdraw_dispute(match: Match, *, actor: Actor) -> Match:
    """A participant withdraws their open dispute of ``match`` (#267).

    The withdrawal is a new ``withdraw`` row; the dispute row stays. The
    match stays disputed while another dispute of the current period is
    open, with no Audit Event and no signal. When the last open dispute is
    withdrawn, the match goes back to confirmed when it was confirmed as
    the period began (the ``match.dispute`` event's ``before``), else to
    pending, with one ``match.dispute_withdraw`` Audit Event. Back to
    confirmed sends ``match_confirmed``; back to pending sends nothing.

    A withdrawn dispute is still the player's one dispute: disputing again
    changes nothing. Only a participant may withdraw
    (``MatchActionDenied``). A voided match gets ``MatchStateError``
    first, also for a player who already withdrew. On a match that is not
    voided, a repeat withdrawal changes nothing; otherwise a participant
    with no open dispute in the current period, or a match that is not
    disputed, gets ``MatchStateError``. So does every withdrawal when the
    period's opening row is unknown (``_dispute_period``: rows written
    outside the services): the match stays disputed until a resolution or
    expiry.
    """
    _saved(match)
    player = participant_player(actor, match)
    if player is None:
        raise MatchActionDenied
    with transaction.atomic():
        locked = _lock(match)
        # A voided match is refused first, as ``dispute_match`` does, even
        # for a player who already withdrew.
        if locked.status == MatchStatus.VOIDED:
            raise MatchStateError
        if MatchConfirmation.objects.filter(
            match=locked, player=player, decision=ConfirmationDecision.WITHDRAW
        ).exists():
            return locked
        if locked.status != MatchStatus.DISPUTED:
            raise MatchStateError
        period = _dispute_period(locked)
        event, open_disputes = period.event, period.open_disputes
        # With no opening row the period's disputes are unknown, so no
        # withdrawal may end it (fail closed): a resolution or expiry does.
        if event is None or period.opening_pk is None:
            raise MatchStateError
        if not any(row.player_id == player.pk for row in open_disputes):
            raise MatchStateError
        _record_decision(locked, player, ConfirmationDecision.WITHDRAW)
        if any(row.player_id != player.pk for row in open_disputes):
            return locked
        was_confirmed = (event.before or {}).get("status") == MatchStatus.CONFIRMED
        status = MatchStatus.CONFIRMED if was_confirmed else MatchStatus.PENDING
        before = _set_status(locked, status)
        record_event(
            action=AuditAction.MATCH_DISPUTE_WITHDRAW,
            target=locked,
            before=before,
            after=snapshot(locked, ["status"]),
            **audit_actor(actor),
        )
        if status == MatchStatus.CONFIRMED:
            match_confirmed.send(sender=Match, match=locked)
        return locked


_DISPUTE_REASON_REQUIRED = "Give a reason for the dispute outcome."


def resolve_dispute(
    match: Match,
    *,
    actor: Actor,
    outcome: str,
    reason: str,
    as_moderator: bool = True,
) -> Match:
    """Resolve a disputed match as confirmed or voided.

    For the match's Event's verified Organizer (``settles_dispute``: a
    member whose Player is not in the match's correction chain, or the
    approved application that submitted the match and still acts for that
    Organizer, ADR 0034), or a moderator (``moderates``). ``reason`` is
    required. Records one ``dispute.outcome`` Audit Event. A confirmed
    outcome sends ``match_confirmed``; a voided one sends ``release`` for
    each match it no longer holds. With ``as_moderator=False`` (the
    Organizer's API operations) the moderator path is never taken: only
    ``settles_dispute`` lets the actor resolve.
    """
    _saved(match)
    moderator = as_moderator and moderates(actor, match)
    if not (moderator or settles_dispute(actor, match)):
        raise MatchActionDenied
    if outcome not in _RESOLUTIONS:
        raise ValueError("Resolve a dispute as confirmed or voided.")
    if not reason.strip():
        raise ValueError(_DISPUTE_REASON_REQUIRED)
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


_REASON_REFUSED = (
    "Only a moderator, or an organizer settling a dispute, may give a reason."
)


class ReasonRefused(ValueError):
    """An actor not acting as moderator, nor as the Organizer settling a
    dispute, gave a reason. Fixed text."""

    def __init__(self) -> None:
        super().__init__(_REASON_REFUSED)


def _check_reason(moderator: bool, reason: str) -> None:
    """Refuse the reason of an actor who may not give one, with fixed text.

    Audit Events are append-only, so free text that could hold a name is
    stored only from moderators and from the Event's verified Organizer
    settling a dispute (ADR 0034). ``moderator`` is True for either.
    """
    if not moderator and reason.strip():
        raise ReasonRefused


def _void(
    locked: Match,
    *,
    action: AuditAction,
    actor: Actor | None,
    reason: str,
    rule: str | None = None,
) -> None:
    """Void a locked match, audit it, and send its signals.

    ``rule``, when given, names the expiry rule in the Audit Event's
    ``after``.
    """
    was_counting = locked.is_counted()
    linked = _counting(_linked(locked))
    before = _set_status(locked, MatchStatus.VOIDED)
    after = snapshot(locked, ["status"])
    if rule is not None:
        after["rule"] = rule
    record_event(
        action=action,
        target=locked,
        before=before,
        after=after,
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
    gets ``ReasonRefused``. A match that was counting sends
    ``match_changed`` with ``change=void``, and each match it no longer
    holds gets ``release``.
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
    as_moderator: bool = True,
    as_player: bool = False,
) -> Match:
    """Void ``match`` and create its correction. Returns the new match.

    For a moderator, or an actor ``can_change_match`` allows that may also
    submit the correction (``can_create_match`` for the old match's Event
    and Club and the new players), checked before any validation. Off the
    player path, only a moderator, or the Event's verified Organizer
    settling the dispute (``settles_dispute`` with the new players, ADR
    0034), edits a disputed match; anyone else gets ``MatchStateError``,
    as for a voided match. On the player path (``as_player=True``, below)
    a participant also edits a disputed self-reported match.
    With ``as_moderator=False`` (the Organizer's API operations) the
    moderator path is never taken, so only the non-moderator rules apply.
    For a match with an Event, the write transaction first share-locks the
    Event row and checks a non-moderator again (``hold_event``), so an
    organizer change made after the first check refuses the edit with
    ``MatchActionDenied`` before any lock or write. The correction keeps
    the old match's stored match type, never the Event's current kind.

    The correction keeps the old match's external id, Event, and Club,
    and links to it through ``corrects``. Off the player path it keeps the
    old match's submitter (ADR 0013) and is confirmed when the old one was
    confirmed or disputed, else pending. On the player path its submitter
    is the corrector's Account (the maintainer's decision on #267), and it
    is always pending, with the corrector's ``confirm`` row. It goes
    through validation, the model rules, and the duplicate checks
    (``MatchValidationError``, ``DuplicateMatchError``), and any error
    rolls everything back. One ``match.edit`` Audit Event on the old match
    records it (no separate ``match.void``).

    With ``as_player=True`` (the players' correction operation, #267) only
    the player rules apply: a participant of a self-reported match (no
    Event, no Club) corrects a pending or disputed one
    (``corrects_as_player``). The correction must have the same two teams,
    in either order, or the edit is refused with ``MatchActionDenied``
    before any validation. A confirmed or voided match gets
    ``MatchStateError`` (dispute it first). The correction is always
    pending, with the corrector's ``confirm`` row written in the same
    transaction, so it counts only once a player on the other team
    confirms it. The Audit Event's ``after`` then also names the
    correction's status (``correction_status``). Without ``as_player`` a
    participant gets ``MatchActionDenied``, as before.

    Only a moderator, or the settling Organizer, may give a ``reason``;
    anyone else giving one gets ``ReasonRefused``. The settling Organizer
    must give one (``ValueError``), and a match that is no longer disputed
    under the lock refuses it (``MatchStateError``).

    Signals: ``match_changed`` with ``change=edit`` for an old match that
    was counting (the earlier of the two dates), else ``match_confirmed``
    for a confirmed correction, and ``hold`` or ``release`` for each other
    match whose counting changed.
    """
    _saved(match)
    new_player_ids = [
        player.pk
        for player in (*team_a, *team_b)
        if isinstance(player, Player) and player.pk is not None
    ]
    # A moderator who played in the match, or who puts their own Player in
    # the correction, falls back to the other rules.
    # With as_moderator=False (the Organizer's API operations) only the
    # non-moderator rules apply, whoever the actor is.
    # With as_player=True (the players' correction operation) only the
    # player rules apply (#267): never the moderator or Organizer paths.
    moderator = (
        as_moderator
        and not as_player
        and moderates(actor, match, new_player_ids=new_player_ids)
    )
    player_path = as_player
    if player_path:
        if not can_correct_as_player(actor, match):
            raise MatchActionDenied
    elif not (moderator or can_change_match(actor, match)):
        raise MatchActionDenied
    team_ids = [
        [p.pk for p in team if isinstance(p, Player) and p.pk is not None]
        for team in (team_a, team_b)
    ]
    corrector: Player | None = None
    if player_path:
        corrector = corrects_as_player(
            actor, match, team_a_ids=team_ids[0], team_b_ids=team_ids[1]
        )
        if corrector is None:
            raise MatchActionDenied
    stored = Match.objects.select_related(
        "submitted_by_user", "submitted_by_application", "event", "club"
    ).get(pk=match.pk)
    submission = MatchSubmission(
        event=stored.event,
        club=stored.club,
        player_ids=frozenset(new_player_ids),
    )

    def check_editor() -> None:
        # An edit creates a match, so the editor also needs the authority
        # to submit it, checked before any validation.
        if moderator:
            return
        if player_path:
            allowed = corrects_as_player(
                actor, match, team_a_ids=team_ids[0], team_b_ids=team_ids[1]
            ) is not None and can_create_match(actor, submission)
        else:
            allowed = can_change_match(actor, match) and can_create_match(
                actor, submission
            )
        if not allowed:
            raise MatchActionDenied

    check_editor()
    # The Event's verified Organizer settling a dispute (ADR 0034) gives a
    # reason, as a moderator does. The status read here only decides
    # whether a reason may be given; it is checked again under the lock.
    settles = (
        not moderator
        and not player_path
        and stored.status == MatchStatus.DISPUTED
        and settles_dispute(actor, match, new_player_ids=new_player_ids)
    )
    _check_reason(moderator or settles, reason)
    if settles and not reason.strip():
        raise ValueError(_DISPUTE_REASON_REQUIRED)
    draft = draft_match(
        format=format,
        team_a=team_a,
        team_b=team_b,
        games=games,
        played_on=played_on,
        best_of=best_of,
        game_target=game_target,
        scoring_type=scoring_type,
        # A player's correction is submitted by the corrector (the
        # maintainer's decision on #267); every other edit keeps the old
        # match's submitter (ADR 0013).
        submitted_by_user=(
            corrector.account if corrector is not None else stored.submitted_by_user
        ),
        submitted_by_application=(
            None if corrector is not None else stored.submitted_by_application
        ),
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
        # Holds the Event fixed until commit and checks the editor again.
        # The correction keeps the old match's type (the maintainer's
        # decision on #163).
        hold_event(draft, check_editor, keep_type_of=stored)
        lock_draft(draft, also=[stored_lineup_lock_key(stored)])
        locked = _lock_row(match)
        if locked.status == MatchStatus.VOIDED:
            raise MatchStateError
        if player_path:
            # A player corrects only a pending or disputed match: a
            # confirmed one is disputed first.
            if locked.status == MatchStatus.CONFIRMED:
                raise MatchStateError
        # Only a moderator or the settling Organizer edits a disputed match.
        # A match no longer disputed under the lock refuses the settling
        # Organizer's reason: a reason is taken only to settle a dispute.
        elif not moderator and (locked.status == MatchStatus.DISPUTED) != settles:
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
            if player_path or old_status == MatchStatus.PENDING
            else MatchStatus.CONFIRMED
        )
        corrected, _held = insert_draft(draft, status=new_status, corrects=locked)
        if corrector is not None:
            # The corrector's own confirmation, as a submission is the
            # submitter's (#197). One team never confirms a self-reported
            # match, so the correction waits for the other team.
            _record_decision(corrected, corrector, ConfirmationDecision.CONFIRM)
        after: dict[str, object] = {
            **snapshot(locked, ["status"]),
            "corrected_by": corrected.public_code,
        }
        if player_path:
            # A player's correction of a disputed match is created pending,
            # so the rating adapter must not read ``before`` alone as a
            # correction created confirmed (ADR 0024).
            after["correction_status"] = corrected.status
        record_event(
            action=AuditAction.MATCH_EDIT,
            target=locked,
            before=before,
            after=after,
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


def _resolve_flag_rows(
    flags: Sequence[MatchFlag],
    *,
    outcome: FlagResolution,
    reason: str,
    actor: Actor,
    rule: str | None = None,
) -> None:
    """Resolve locked open flags as ``outcome``, one Audit Event each.

    Sets ``resolution`` and ``resolved_at`` (one time for all of them) and
    writes one ``match.flag_resolve`` Audit Event per flag, with
    ``reason``. ``rule``, when given, names why in the event's ``after``
    (``vouched`` for ``vouch_for_match``). The caller holds the locks and
    sends the count change signals.
    """
    now = timezone.now()
    for flag in flags:
        before = snapshot(flag, ["resolution", "resolved_at"])
        flag.resolution = outcome
        flag.resolved_at = now
        flag.save(update_fields=["resolution", "resolved_at"])
        after = snapshot(flag, ["resolution", "resolved_at"])
        if rule is not None:
            after["rule"] = rule
        record_event(
            action=AuditAction.MATCH_FLAG_RESOLVE,
            target=flag,
            before=before,
            after=after,
            reason=reason,
            **audit_actor(actor),
        )


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
        _resolve_flag_rows(
            flags, outcome=FlagResolution(outcome), reason=reason, actor=actor
        )
        _send_count_changes(counting)
        return len(flags)


def _open_holds_of(match: Match) -> QuerySet[MatchFlag]:
    """The open possible duplicate flags that hold ``match`` and whose
    other match is not voided: the flags a vouch for ``match`` covers."""
    return (
        MatchFlag.objects.filter(
            Q(match=match) | Q(duplicate_of=match),
            reason=FlagReason.POSSIBLE_DUPLICATE,
            resolved_at__isnull=True,
        )
        .exclude(
            Q(match=match, duplicate_of__status=MatchStatus.VOIDED)
            | Q(duplicate_of=match, match__status=MatchStatus.VOIDED)
        )
        .order_by("pk")
    )


def _other_side(flag: MatchFlag, match_pk: int) -> int:
    other = flag.duplicate_of_id if flag.match_id == match_pk else flag.match_id
    assert other is not None
    return other


def _flag_vouched(flag: MatchFlag, match_pk: int) -> bool:
    """True when match ``match_pk`` of ``flag`` is vouched for at its level.

    Levels are decided per match, from the stored match: an Event match
    by any vouch row (only its Organizer's actors write one), a
    self-reported match by vouches from Accounts whose Players play on
    each team, and a legacy club-only match never.
    """
    links = Match.objects.filter(pk=match_pk).values("event_id", "club_id").get()
    vouches = MatchVouch.objects.filter(flag=flag, match_id=match_pk)
    if links["event_id"] is not None:
        return vouches.exists()
    if links["club_id"] is not None:
        return False
    teams = set(
        MatchParticipant.objects.filter(
            match_id=match_pk,
            player__account_id__in=vouches.filter(account__isnull=False).values(
                "account_id"
            ),
        ).values_list("team", flat=True)
    )
    return teams >= {Team.A, Team.B}


def vouch_for_match(match: Match, *, actor: Actor) -> Match:
    """``actor`` vouches that ``match`` is not a duplicate (ADR 0034, #270).

    For an actor ``vouch_level`` allows: an Event match is vouched for by
    its Event's verified Organizer (``settles_dispute``), a self-reported
    match by its players. Anyone else gets ``MatchActionDenied`` first.
    Under the match's lineup and row locks, a voided match, and a match
    that no open possible duplicate flag holds (one whose other match is
    not voided), raise ``MatchStateError``.

    The vouch covers every open flag that holds the match: for each one the
    actor has not vouched on yet, it writes one ``MatchVouch`` row and one
    ``match.vouch`` Audit Event. A flag opened later needs a new vouch. A
    flag is resolved as approved (``_resolve_flag_rows``, rule
    ``vouched``, no reason, the actor whose vouch completed it as its
    actor) once both its matches are vouched for at their own levels
    (``_flag_vouched``), and each match that counts again sends
    ``release``. Every open flag is checked on each vouch, a repeat
    included. A repeat vouch writes no vouch row and no ``match.vouch``
    event. A vouch takes no text: the audit log cannot be erased. A vouch
    row keeps counting after its voucher loses standing (a member removed,
    an Organizer unverified, an application unapproved, an Account
    deactivated), as a confirmation row does. Returns the locked match.
    """
    _saved(match)
    level = vouch_level(actor, match)
    if level is None:
        raise MatchActionDenied
    voucher: dict[str, object] = (
        {"application": actor}
        if isinstance(actor, get_application_model())
        else {"account": actor}
    )
    with transaction.atomic():
        # Flags link only matches with the same lineup and date, so the
        # match's lineup lock covers the other match of every flag.
        locked = _lock(match)
        if locked.status == MatchStatus.VOIDED:
            raise MatchStateError
        flags = list(_open_holds_of(locked).select_for_update(of=("self",)))
        if not flags:
            raise MatchStateError
        counting = _counting([locked, *_linked(locked)])
        completed: list[MatchFlag] = []
        for flag in flags:
            if not MatchVouch.objects.filter(
                flag=flag, match=locked, **voucher
            ).exists():
                row = MatchVouch.objects.create(flag=flag, match=locked, **voucher)
                record_event(
                    action=AuditAction.MATCH_VOUCH,
                    target=row,
                    after={
                        "flag": flag.pk,
                        "match": locked.public_code,
                        "level": str(level),
                    },
                    **audit_actor(actor),
                )
            # Checked for every open flag, also after a repeat vouch, so a
            # flag both sides already vouched for is always resolved.
            if _flag_vouched(flag, locked.pk) and _flag_vouched(
                flag, _other_side(flag, locked.pk)
            ):
                completed.append(flag)
        if completed:
            _resolve_flag_rows(
                completed,
                outcome=FlagResolution.APPROVED,
                reason="",
                actor=actor,
                rule="vouched",
            )
        _send_count_changes(counting)
        return locked


def vouched_at_own_level(match: Match, account: "_User") -> bool:
    """True when ``account``'s level has vouched for ``match`` on every
    open flag that holds it. Read only.

    For an Event match, any vouch on the flag for it (only its Organizer's
    actors write one). For a self-reported match, a vouch from a player
    on ``account``'s own team. False when no open flag holds the match.
    """
    flags = list(_open_holds_of(match))
    if not flags:
        return False
    stored = Match.objects.filter(pk=match.pk).values("event_id").get()
    team = (
        MatchParticipant.objects.filter(match_id=match.pk, player__account=account)
        .values_list("team", flat=True)
        .first()
    )
    teammates = MatchParticipant.objects.filter(match_id=match.pk, team=team).values(
        "player__account_id"
    )
    for flag in flags:
        vouches = MatchVouch.objects.filter(flag=flag, match_id=match.pk)
        if stored["event_id"] is None:
            if team is None:
                return False
            vouches = vouches.filter(account_id__in=teammates)
        if not vouches.exists():
            return False
    return True


def vouchable_holds(account: "_User") -> QuerySet[Match]:
    """The held matches ``account`` may vouch for. Read only.

    Matches that are not voided and that an open possible duplicate flag
    holds (its other match not voided), that ``account`` may vouch for
    (``vouch_level``). An inactive Account may vouch for none. The rule is
    applied in SQL, so the cost does not grow with the held matches:
    self-reported matches its Player plays in (the player level), and
    Event matches of verified Organizers it is a member of that its Player
    does not play in (the Organizer level). Only an Event match with a
    correction chain (it corrects a match, or a match corrects it) is
    checked in Python with ``vouch_level``, since the conflict rule
    follows the chain. Such matches are rare, so the list still calls
    ``vouch_level`` once for each of them.
    """
    if not get_user_model().objects.filter(pk=account.pk, is_active=True).exists():
        return Match.objects.none()
    open_flags = MatchFlag.objects.filter(
        reason=FlagReason.POSSIBLE_DUPLICATE, resolved_at__isnull=True
    )
    held = Exists(
        open_flags.filter(match=OuterRef("pk")).exclude(
            duplicate_of__status=MatchStatus.VOIDED
        )
    ) | Exists(
        open_flags.filter(duplicate_of=OuterRef("pk")).exclude(
            match__status=MatchStatus.VOIDED
        )
    )
    plays = Exists(
        MatchParticipant.objects.filter(
            match=OuterRef("pk"), player__account_id=account.pk
        )
    )
    held_matches = Match.objects.exclude(status=MatchStatus.VOIDED).filter(held)
    self_reported = held_matches.filter(plays, event__isnull=True, club__isnull=True)
    organizer = held_matches.filter(
        ~plays,
        Exists(
            OrganizerMember.objects.filter(
                organizer_id=OuterRef("event__organizer_id"),
                organizer__verified_at__isnull=False,
                account_id=account.pk,
            )
        ),
    )
    in_chain = Q(corrects__isnull=False) | Exists(
        Match.objects.filter(corrects_id=OuterRef("pk"))
    )
    chained = [
        candidate.pk
        for candidate in organizer.filter(in_chain).only("pk")
        if vouch_level(account, candidate) is not None
    ]
    return Match.objects.filter(
        Q(pk__in=self_reported.values("pk"))
        | Q(pk__in=organizer.exclude(in_chain).values("pk"))
        | Q(pk__in=chained)
    )


def _conflicted_chain_ids(account_pk: int) -> RawSQL:
    """SQL for the ids of every match in the correction chain of a match
    the Account's Players play in.

    A chain is the matches linked through ``corrects`` in either direction,
    the set ``permissions._correction_chain`` walks one match at a time.
    One recursive query starts from the Account's matches and follows
    ``corrects`` back and forward, so its cost does not grow with the rows
    a list filters.
    """
    match_table = connection.ops.quote_name(Match._meta.db_table)
    participant_table = connection.ops.quote_name(MatchParticipant._meta.db_table)
    player_table = connection.ops.quote_name(Player._meta.db_table)
    # The table names come from the models and the Account id is a query
    # parameter, so nothing from a request is put into the SQL text.
    sql = f"""
        WITH RECURSIVE chain(id) AS (
            SELECT mp.match_id
            FROM {participant_table} mp
            JOIN {player_table} p ON p.id = mp.player_id
            WHERE p.account_id = %s
            UNION
            SELECT CASE WHEN m.id = chain.id THEN m.corrects_id ELSE m.id END
            FROM {match_table} m
            JOIN chain
              ON (m.id = chain.id AND m.corrects_id IS NOT NULL)
              OR m.corrects_id = chain.id
        )
        SELECT id FROM chain
    """
    return RawSQL(sql, (account_pk,))


def organizer_disputes(account: "_User") -> QuerySet[Match]:
    """The disputed matches ``account`` may settle as an Organizer. Read only.

    Disputed matches of an Event run by a verified Organizer the Account
    is a member of, minus every match whose correction chain holds a
    match the Account's Player plays in: ``settles_dispute`` refuses such
    a member (ADR 0034, the maintainer's decision on #272). The rule runs
    in the database in one query, with no check per row.
    """
    return (
        Match.objects.filter(
            status=MatchStatus.DISPUTED,
            event__organizer__verified_at__isnull=False,
            event__organizer__members__account=account,
        )
        .exclude(pk__in=_conflicted_chain_ids(account.pk))
        .distinct()
    )


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
    expired, failed = _expire_each(candidates, lambda pk: _expire_one(pk, cutoff))
    if expired:
        logger.info("Expired %d unconfirmed matches.", expired)
    if failed:
        raise ExpiryFailed(len(failed))
    return expired


def _expire_each(
    pks: Iterable[int], expire_one: Callable[[int], bool]
) -> tuple[int, list[int]]:
    """Run ``expire_one`` on each match id. Returns (expired, failed ids).

    Each call runs its own transaction. A match that fails rolls back
    alone and is logged by id only; the others still expire. The task's
    soft time limit (``SoftTimeLimitExceeded``) is not a failed match: it
    stops the run at once, keeping the matches already expired.
    """
    expired = 0
    failed: list[int] = []
    for pk in pks:
        try:
            if expire_one(pk):
                expired += 1
        except SoftTimeLimitExceeded:
            # Out of time: stop cleanly. Matches already expired stay
            # committed, and the rest wait for the next run.
            raise
        except Exception:
            # Its transaction has rolled back. Log the id only: the error's
            # text could hold anything. The other matches still expire.
            failed.append(pk)
            logger.error("Could not expire match %s.", pk)
    return expired, failed


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


def _match_events() -> QuerySet[AuditEvent]:
    return AuditEvent.objects.filter(
        target_content_type__app_label=Match._meta.app_label,
        target_content_type__model=Match._meta.model_name,
    )


def dispute_started_at(match: Match) -> datetime | None:
    """When the match's current disputed period began, or None.

    The period's open disputes come from ``_dispute_period``. When the
    first of them (by id) is the period's opening row, the start is the
    time of the match's latest ``match.dispute`` Audit Event, chosen by
    row order, not time: every change into disputed writes exactly one,
    and one match's status changes are serialized by its locks, so the
    highest id is the latest. When the opener withdrew and a later dispute
    is still open, the start is that row's time (the first open dispute
    by id). With no event or no open dispute, None. With no opening row
    (the event names no Account, or its Player has no dispute row: rows
    written outside the services), the start is the event's time, the
    later and safe start: a dispute row from an earlier settled period
    never expires the match early. Times are never compared to choose a
    row, so a host with a clock behind another cannot hide a dispute.
    """
    period = _dispute_period(match)
    if period.event is None or not period.open_disputes:
        return None
    started: datetime = period.event.created_at
    first = period.open_disputes[0]
    if period.opening_pk is None or first.pk == period.opening_pk:
        return started
    return first.created_at


def expire_disputes_and_holds(now: datetime | None = None) -> int:
    """Void unsettled disputes and possible duplicate holds (ADR 0034).

    The dispute rule voids a disputed match whose first open dispute
    (``dispute_started_at``) is older than ``MATCH_DISPUTE_EXPIRY_DAYS``.
    The hold rule then voids the newer match of each open possible
    duplicate flag older than ``MATCH_DUPLICATE_HOLD_DAYS`` whose earlier
    match is confirmed and whose newer match is not voided. Which match is
    newer is decided by ``_hold_sides``: a correction keeps its
    original's place while its chain keeps the same teams and date, so the
    flag's ``match`` is usually the newer one, but not when it corrects a
    match with the same teams and date recorded before ``duplicate_of``.
    That ends the flag's hold on the earlier match, which counts again,
    and sends ``release``, unless another open flag still holds it. A hold
    against a pending or disputed earlier match never voids the newer one
    under this rule. The flag stays open: only ``resolve_flags`` and
    ``vouch_for_match`` resolve flags, and a voided match holds nothing.

    Each match is voided in its own transaction, as
    ``expire_unconfirmed_matches`` does: lineup lock tried, never waited
    for, then the row lock with skip locked, then the rule checked again.
    One ``match.expire`` Audit Event, no actor, whose ``after`` names the
    rule. Running twice is safe. Returns how many matches were voided. A
    match that fails is logged by id and skipped; after both rules ran,
    the run raises ``ExpiryFailed`` with how many failed.
    """
    now = now or timezone.now()
    dispute_cutoff = now - timedelta(days=settings.MATCH_DISPUTE_EXPIRY_DAYS)
    hold_cutoff = now - timedelta(days=settings.MATCH_DUPLICATE_HOLD_DAYS)
    # A cheap superset: any match.dispute event, or any dispute row, before
    # the cutoff. ``dispute_started_at`` returns either the latest event's
    # time or an open dispute row's time, so both are covered. It is
    # checked for each match under its locks.
    disputed = list(
        Match.objects.filter(status=MatchStatus.DISPUTED)
        .filter(
            Exists(
                _match_events().filter(
                    action=AuditAction.MATCH_DISPUTE,
                    target_object_id=Cast(OuterRef("pk"), output_field=CharField()),
                    created_at__lt=dispute_cutoff,
                )
            )
            | Exists(
                MatchConfirmation.objects.filter(
                    match=OuterRef("pk"),
                    decision=ConfirmationDecision.DISPUTE,
                    created_at__lt=dispute_cutoff,
                )
            )
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    expired, failed = _expire_each(
        disputed, lambda pk: _expire_dispute(pk, dispute_cutoff)
    )
    # A match the dispute pass failed is not tried again, so it counts and
    # is logged once.
    held = [pk for pk in _expired_hold_targets(hold_cutoff) if pk not in failed]
    held_expired, held_failed = _expire_each(
        held, lambda pk: _expire_hold(pk, hold_cutoff)
    )
    expired += held_expired
    failed += held_failed
    if expired:
        logger.info("Expired %d disputed or held matches.", expired)
    if failed:
        raise ExpiryFailed(len(failed))
    return expired


def _open_holds(cutoff: datetime) -> QuerySet[MatchFlag]:
    """Open possible duplicate flags older than ``cutoff``, neither side voided.

    The hold rule may end these; ``_hold_target`` decides which, if any.
    """
    return MatchFlag.objects.filter(
        reason=FlagReason.POSSIBLE_DUPLICATE,
        resolved_at__isnull=True,
        created_at__lt=cutoff,
    ).exclude(
        Q(match__status=MatchStatus.VOIDED) | Q(duplicate_of__status=MatchStatus.VOIDED)
    )


_Lineup = tuple[frozenset[frozenset[int]], date | None]


def _lineup(pk: int) -> _Lineup:
    """The stored teams (in either order) and date of match ``pk``.

    A match with no participants has no teams, and ``_place`` never walks
    back from one (fail closed). ``record_match()`` never makes one.
    """
    played_on: date | None = (
        Match.objects.filter(pk=pk).values_list("played_on", flat=True).first()
    )
    teams: dict[str, set[int]] = {}
    for player_id, team in MatchParticipant.objects.filter(match_id=pk).values_list(
        "player_id", "team"
    ):
        teams.setdefault(team, set()).add(player_id)
    return frozenset(frozenset(players) for players in teams.values()), played_on


def _place(pk: int, places: dict[int, int]) -> int:
    """The hold rule's place of match ``pk``: the id it ranks by.

    A correction keeps its original's place only while its chain keeps
    the same teams (in either order) and date (maintainer's decisions on
    #268). So this follows ``corrects`` back from ``pk`` and stops at the
    first step whose lineup or ``played_on`` differs from ``pk``'s, or at
    the match that corrects nothing, and returns that match's id. An edit
    that moves a match onto another lineup or date gets no old place, so
    it cannot take an earlier place than a result already recorded there.
    Ids are given in the order matches are recorded, so a lower place is
    the earlier result. ``corrects``, teams, dates, and ids never change,
    so no lock is needed to read them. ``places`` caches answers for one
    run.
    """
    if pk in places:
        return places[pk]
    lineup = _lineup(pk)
    current = pk
    seen = {pk}
    while True:
        previous: int | None = (
            Match.objects.filter(pk=current)
            .values_list("corrects_id", flat=True)
            .first()
        )
        if (
            previous is None
            or previous in seen
            or not lineup[0]
            or _lineup(previous) != lineup
        ):
            break
        seen.add(previous)
        current = previous
    places[pk] = current
    return current


@dataclass(frozen=True)
class _Hold:
    """One open possible duplicate flag's two matches and their statuses."""

    match_id: int
    match_status: str
    duplicate_of_id: int
    duplicate_of_status: str


def _holds(flags: QuerySet[MatchFlag]) -> list[_Hold]:
    return [
        _Hold(*row)
        for row in flags.values_list(
            "match_id", "match__status", "duplicate_of_id", "duplicate_of__status"
        )
    ]


def _hold_sides(hold: _Hold, places: dict[int, int]) -> tuple[int, int]:
    """The flag's (earlier, newer) match ids for the hold rule.

    Each side ranks by its place (``_place``): the first match of its own
    correction chain with the same teams and date. The lower place is the
    earlier match. The flag's ``duplicate_of`` was recorded before its
    ``match``, so without a correction, and for equal places,
    ``duplicate_of`` is the earlier one. A correction of a match with the
    same teams and date recorded before ``duplicate_of``'s place is the
    earlier one, so the hold rule never voids it for having been
    corrected.
    """
    if _place(hold.match_id, places) < _place(hold.duplicate_of_id, places):
        return hold.match_id, hold.duplicate_of_id
    return hold.duplicate_of_id, hold.match_id


def _hold_target(hold: _Hold, places: dict[int, int]) -> int | None:
    """The id of the match the hold rule voids for ``hold``, or None.

    The newer match (``_hold_sides``), and only when the earlier one is
    confirmed (the maintainer's decision on #268 and #265): a hold
    against a pending or disputed earlier match never voids the newer
    one, or a player's own pending self-submission could void a verified
    Organizer's result. ``hold`` comes from ``_open_holds``, so neither
    side is voided.
    """
    earlier, newer = _hold_sides(hold, places)
    statuses = {
        hold.match_id: hold.match_status,
        hold.duplicate_of_id: hold.duplicate_of_status,
    }
    if statuses[earlier] != MatchStatus.CONFIRMED:
        return None
    return newer


def _expired_hold_targets(cutoff: datetime) -> list[int]:
    """Ids of the matches the hold rule voids now, in id order. Read only.

    Checked again for each match under its locks (``_expire_hold``).
    """
    places: dict[int, int] = {}
    targets = {
        target
        for hold in _holds(_open_holds(cutoff))
        if (target := _hold_target(hold, places)) is not None
    }
    return sorted(targets)


def _lock_for_expiry(pk: int, status: Q) -> Match | None:
    """Try the match's lineup lock, then its row lock, never waiting.

    Returns None when either is held elsewhere or the match's status fails
    ``status``. Call inside a transaction.
    """
    if not try_advisory_lock(stored_lineup_lock_key(Match(pk=pk))):
        return None
    locked: Match | None = (
        Match.objects.select_for_update(skip_locked=True).filter(status, pk=pk).first()
    )
    return locked


def _expire_dispute(pk: int, cutoff: datetime) -> bool:
    """Void one match whose open dispute began before ``cutoff``."""
    with transaction.atomic():
        locked = _lock_for_expiry(pk, Q(status=MatchStatus.DISPUTED))
        if locked is None:
            return False
        started = dispute_started_at(locked)
        if started is None or started >= cutoff:
            return False
        _void(
            locked,
            action=AuditAction.MATCH_EXPIRE,
            actor=None,
            reason="",
            rule="dispute",
        )
        return True


def _expire_hold(pk: int, cutoff: datetime) -> bool:
    """Void the newer match of a possible duplicate flag held past ``cutoff``.

    The match may be either side of the flag (``_hold_sides``). The lineup
    lock also covers the flag's other match (the same teams and date), so
    its status cannot change meanwhile.
    """
    with transaction.atomic():
        locked = _lock_for_expiry(pk, ~Q(status=MatchStatus.VOIDED))
        if locked is None:
            return False
        holds = _holds(
            _open_holds(cutoff).filter(Q(match=locked) | Q(duplicate_of=locked))
        )
        places: dict[int, int] = {}
        if not any(_hold_target(hold, places) == locked.pk for hold in holds):
            return False
        _void(
            locked,
            action=AuditAction.MATCH_EXPIRE,
            actor=None,
            reason="",
            rule="duplicate_hold",
        )
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


def _stored(instance: models.Model, model: type[models.Model]) -> bool:
    """True when ``instance`` was loaded or saved and its row still exists.

    An instance built in memory (``_state.adding``) is refused even with
    an existing row's pk: the audit log refuses it as a target, so the
    run would end with an error after voiding its matches. ``model`` is
    the concrete model, never ``type(instance)``, which a lazy
    ``request.user`` wrapper would hide.
    """
    if instance.pk is None or instance._state.adding:
        return False
    return model._default_manager.filter(pk=instance.pk).exists()


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
    is read or written. Then a blank ``reason``, and a batch that is
    unsaved, built in memory, or has no stored row, raise ``ValueError``
    (fixed text), before any match is read or voided. Next, in its own
    transaction, the batch's Results File, when it has one still pending
    (a CSV upload, #71), fails as ``voided`` under its row lock, keeping
    its row results (``apps.matches.results_files.fail_for_void``), so no
    run of that file records a row afterward. The matches are found by the batch link
    (``submission_batch_matches``), never by time. See ``_bulk_void`` for
    how each is voided and what is recorded.
    """
    # Imported here: results_files imports the services this module uses.
    from apps.matches.results_files import fail_for_void

    _check_bulk(actor, reason)
    if not _stored(batch, SubmissionBatch):
        raise ValueError(_UNSAVED_BATCH)
    fail_for_void(batch.pk)
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
    the same order; a submitter that is unsaved, built in memory, or has
    no stored row, or one that is neither an Account nor a Partner
    Application, raises ``ValueError`` before any match is read or voided.
    Next, each pending Results File of the submitter (a CSV upload, #71)
    fails as ``voided`` in its own transaction, keeping its row results
    (``apps.matches.results_files.fail_for_submitter_void``), so no run of
    it records a row afterward.
    """
    # Imported here: results_files imports the services this module uses.
    from apps.matches.results_files import fail_for_submitter_void

    _check_bulk(actor, reason)
    account_model = get_user_model()
    application_model = get_application_model()
    if isinstance(submitter, account_model):
        stored = _stored(submitter, account_model)
    elif isinstance(submitter, application_model):
        stored = _stored(submitter, application_model)
    else:
        stored = False
    if not stored:
        raise ValueError(_UNKNOWN_SUBMITTER)
    if isinstance(submitter, account_model):
        fail_for_submitter_void(user_id=submitter.pk)
    else:
        fail_for_submitter_void(application_id=submitter.pk)
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

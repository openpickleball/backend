"""Store rating changes and replays (#45, ADR 0025).

``apply_rating()`` is the only way to change a ``RatingState``, and with
``record_replay()`` the only way to write ``RatingHistory`` and
``RatingReplay`` rows. Rating changes are a security-sensitive area (hard
rule 8).

The values stored are the engine's (``engine_adapter.RatingSummary``):
nothing here computes or rounds a rating (hard rule 2). Every change
records one ``rating.change`` Audit Event in the same transaction, with no
actor (the system). Applying the values already stored, or a live update
a Match already made, changes nothing, so a repeated live update task is
harmless. A redelivered replay change is not caught that way: ordering
replay changes against live updates, a redelivered replay task included,
is the replay caller's job, ``replay_ratings()``.

``update_ratings_for_match()`` is the live update (#46, ADR 0027): it
records one confirmed Match on the stored engine snapshot and applies the
changes through ``apply_rating()``. ``schedule_replay()`` is where a late
match, or one the snapshot cannot continue, goes: it stores a replay
request through ``request_replay()``, as a change to what counts does
(``match_changed``, #47).

``replay_ratings()`` is the replay (#47): a full replay of the counted
ledger that applies every rating through ``apply_rating()`` and removes
the ratings it no longer produces (``_remove_rating()``, the only path
that deletes a ``RatingState``). It orders replay changes against live
updates: both lock the engine snapshot row first, so they serialize, and
it runs only while a request is pending, so a redelivered or repeated
replay task writes nothing.

``recompute_ratings()`` is the recompute (#48, ADR 0058), behind
``manage.py recompute_ratings``: the same full replay without a request,
with a dry run that writes nothing.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from django.conf import settings
from django.db import connection, models, transaction
from django.db.models import Max
from django.utils import timezone
from kombu.exceptions import OperationalError

from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot
from apps.matches.models import (
    Match,
    MatchFormat,
    MatchParticipant,
    RatingFormat,
)
from apps.players.models import Player
from apps.ratings.engine_adapter import (
    LiveConfig,
    LiveUpdate,
    RatingSummary,
    ReplayProvenance,
    ReplayRun,
    counted_engine_matches,
    engine_version,
    live_config,
    record_live,
    run_replay,
    snapshot_holds_match,
)
from apps.ratings.models import (
    EngineSnapshot,
    RatingChangeKind,
    RatingHistory,
    RatingReplay,
    RatingReplayRequest,
    RatingState,
    ReplayReason,
)

logger = logging.getLogger(__name__)

# What publishing a task raises when the broker cannot take it: kombu's
# connection error, or a socket error (ConnectionError is one).
_BROKER_ERRORS = (OperationalError, OSError)

# The stored summary fields and the versions that produced them. They
# decide whether an apply changes anything, and they are what the Audit
# Event records before and after.
SUMMARY_FIELDS: tuple[str, ...] = (
    "rating",
    "rd",
    "reliability",
    "imported_reliability",
    "established",
    "matches_played",
    "last_played",
    "calculated_as_of",
    "live_updates_since_recalculation",
    "config_version",
    "params_version",
    "parameter_fingerprint",
)
VALUE_FIELDS: tuple[str, ...] = (*SUMMARY_FIELDS, "engine_version", "spec_version")


class RatingApplyError(ValueError):
    """``apply_rating()`` refused its input. The message is fixed text."""


def _is_saved(instance: Player | Match | RatingReplay) -> bool:
    return instance.pk is not None and not instance._state.adding


def _check(
    *,
    player: Player,
    summary: RatingSummary,
    kind: RatingChangeKind,
    match: Match | None,
    replay: RatingReplay | None,
    delta: float | None,
) -> None:
    """Refuse input that names the wrong player, format, or cause."""
    if not _is_saved(player):
        raise RatingApplyError("The player must be saved.")
    if summary.player_id != player.public_code:
        raise RatingApplyError("The summary is for another player.")
    if summary.rating_format not in RatingFormat.values:
        raise RatingApplyError("The rating format is not known.")
    if match is not None and not _is_saved(match):
        raise RatingApplyError("The match must be saved.")
    if replay is not None and not _is_saved(replay):
        raise RatingApplyError("The replay must be saved.")
    if kind == RatingChangeKind.LIVE_UPDATE:
        if match is None or replay is not None:
            raise RatingApplyError("A live update names its match and no replay.")
    elif kind == RatingChangeKind.REPLAY:
        if replay is None or match is not None:
            raise RatingApplyError("A replay change names its replay and no match.")
        if delta is not None:
            raise RatingApplyError("Only a live update has a delta.")
        # The summary must come from this replay: its date, config, and no
        # live updates since.
        if (
            summary.calculated_as_of != replay.as_of
            or summary.config_version != replay.config_version
            or summary.params_version != replay.params_version
            or summary.parameter_fingerprint != replay.parameter_fingerprint
            or summary.live_updates_since_recalculation != 0
        ):
            raise RatingApplyError("The summary does not come from this replay.")
    elif kind == RatingChangeKind.REMOVED:
        raise RatingApplyError("A removal goes through the replay.")
    else:
        raise RatingApplyError("The kind of change is not known.")
    if match is None:
        return
    if not MatchParticipant.objects.filter(match=match, player=player).exists():
        raise RatingApplyError("The player did not play in the match.")
    # Only a Match that counts moves a rating (hard rule 8).
    if not Match.objects.counted().filter(pk=match.pk).exists():
        raise RatingApplyError("The match does not count.")
    # The engine feeds a category doubles match (mixed, men's, women's)
    # into overall doubles too, so a doubles match may update either its
    # own format or overall doubles.
    allowed = {match.rating_format}
    if match.format == MatchFormat.DOUBLES:
        allowed.add(RatingFormat.DOUBLES)
    if summary.rating_format not in allowed:
        raise RatingApplyError("The match does not count in this rating format.")


def record_replay(
    provenance: ReplayProvenance, *, spec_version: str | None = None
) -> RatingReplay:
    """Store one replay's provenance (hard rule 4) and return it.

    The spec version is ``spec_version`` when given (the recompute, #48,
    passes the version it replays under), otherwise
    ``settings.OPEN_RATING_SPEC_VERSION``. Raises ``ValidationError`` for a
    value the model refuses, before any write. No Audit
    Event: a replay itself changes no rating, and each rating it changes
    records its own through ``apply_rating()``.
    """
    replay = RatingReplay(
        as_of=provenance.as_of,
        config_version=provenance.config_version,
        config_fingerprint=provenance.config_fingerprint,
        params_version=provenance.params_version,
        parameter_fingerprint=provenance.parameter_fingerprint,
        matches_considered=provenance.matches_considered,
        matches_counted=provenance.matches_counted,
        # Copied, never coerced: a value that is not a mapping must reach
        # the model's check as it is.
        net_rating_created=(
            dict(provenance.net_rating_created)
            if isinstance(provenance.net_rating_created, dict)
            else provenance.net_rating_created
        ),
        engine_version=provenance.engine_version,
        spec_version=(
            settings.OPEN_RATING_SPEC_VERSION if spec_version is None else spec_version
        ),
    )
    replay.full_clean()
    replay.save()
    return replay


def apply_rating(
    *,
    player: Player,
    summary: RatingSummary,
    kind: RatingChangeKind,
    match: Match | None = None,
    replay: RatingReplay | None = None,
    delta: float | None = None,
) -> RatingHistory | None:
    """Store the engine's ``summary`` as ``player``'s rating in its format.

    A live update names the ``match`` that caused it and may give the
    engine's ``delta`` for the player; a replay change names its
    ``replay``. The versions stored are the replay's for a replay change,
    and otherwise the installed engine's and
    ``settings.OPEN_RATING_SPEC_VERSION``.

    Creates or changes the ``RatingState``, appends one ``RatingHistory``
    row, and records one ``rating.change`` Audit Event, in one transaction,
    and returns the history row. It writes nothing and returns None when
    every value equals the stored state, and for a live update when this
    Match already updated this player in this format (a redelivery, even
    after a version change). A redelivered replay change after a newer live
    update is not caught here: it writes another history row and replaces
    the live update's values. Ordering replay changes against live updates,
    a redelivered replay task included, is the replay caller's job (#47).

    Raises ``RatingApplyError`` (fixed text) before any write for input
    that names the wrong player, format, or cause: a live update needs a
    Match that counts (``Match.objects.counted()``), and a replay change
    needs a summary from its replay (the same date, config version,
    parameter set, and fingerprint, and no live updates since). Raises
    ``ValidationError`` for a value the models refuse, before any write.

    It locks the Player row first (``FOR NO KEY UPDATE``, so inserts of
    rows that point at the Player, such as Match Participants, are not
    blocked), so applies for one player serialize, a first apply included,
    and then the state row. The locks last until
    the outermost transaction ends, so the caller chooses the transaction.
    The callers (#46, #47, #48) own ordering, and the lock
    order across players.
    """
    _check(
        player=player,
        summary=summary,
        kind=kind,
        match=match,
        replay=replay,
        delta=delta,
    )
    values: dict[str, Any] = {name: getattr(summary, name) for name in SUMMARY_FIELDS}
    if replay is not None:
        values["engine_version"] = replay.engine_version
        values["spec_version"] = replay.spec_version
    else:
        values["engine_version"] = engine_version()
        values["spec_version"] = settings.OPEN_RATING_SPEC_VERSION

    with transaction.atomic():
        # Serializes applies for one player, even when no state exists yet.
        list(
            Player.objects.select_for_update(no_key=True)
            .filter(pk=player.pk)
            .values_list("pk", flat=True)
        )
        if (
            kind == RatingChangeKind.LIVE_UPDATE
            and RatingHistory.objects.filter(
                player=player,
                rating_format=summary.rating_format,
                match=match,
                kind=RatingChangeKind.LIVE_UPDATE,
            ).exists()
        ):
            return None
        state = (
            RatingState.objects.select_for_update()
            .filter(player=player, rating_format=summary.rating_format)
            .first()
        )
        if state is not None and all(
            getattr(state, name) == values[name] for name in VALUE_FIELDS
        ):
            return None

        before = None if state is None else snapshot(state, VALUE_FIELDS)
        if state is None:
            state = RatingState(
                player=player, rating_format=summary.rating_format, **values
            )
        else:
            for name, value in values.items():
                setattr(state, name, value)
        history = RatingHistory(
            player=player,
            rating_format=summary.rating_format,
            kind=kind,
            match=match,
            replay=replay,
            delta=delta,
            **values,
        )
        state.full_clean()
        history.full_clean()
        state.save()
        history.save()
        record_event(
            action=AuditAction.RATING_CHANGE,
            target=state,
            before=before,
            after={
                **snapshot(state, VALUE_FIELDS),
                "kind": str(kind),
                "match": None if match is None else match.public_code,
                "replay": None if replay is None else replay.pk,
                # The player's OPEN Code: a state's events stay tied to its
                # player after a replay removes the row (the data export,
                # ADR 0051).
                "player": player.public_code,
            },
        )
    return history


def _remove_rating(
    *, player: Player, state: RatingState, replay: RatingReplay
) -> RatingHistory:
    """Delete ``player``'s ``state``, which ``replay`` no longer produces.

    Private to the replay (``replay_ratings()`` and ``recompute_ratings()``):
    the only path that deletes a ``RatingState``. Within the caller's
    transaction it locks the Player row (``FOR NO KEY UPDATE``) and then the state row, appends
    a ``removed`` history row naming ``replay`` with the values removed,
    records one ``rating.change`` Audit Event (``after`` is None) before
    the delete, and then deletes the row through ``Model.delete``, which
    bypasses ``RatingState.delete()``; that and every queryset delete
    keep refusing. Raises ``RatingApplyError`` (fixed text) before any
    write for a state of another player or an unsaved record.
    """
    if not _is_saved(player) or not _is_saved(replay):
        raise RatingApplyError("The player and the replay must be saved.")
    if state.pk is None or state.player_id != player.pk:
        raise RatingApplyError("The rating is for another player.")
    with transaction.atomic():
        list(
            Player.objects.select_for_update(no_key=True)
            .filter(pk=player.pk)
            .values_list("pk", flat=True)
        )
        locked = RatingState.objects.select_for_update().get(pk=state.pk)
        values = {name: getattr(locked, name) for name in VALUE_FIELDS}
        history = RatingHistory(
            player=player,
            rating_format=locked.rating_format,
            kind=RatingChangeKind.REMOVED,
            match=None,
            replay=replay,
            delta=None,
            **values,
        )
        history.full_clean()
        history.save()
        record_event(
            action=AuditAction.RATING_CHANGE,
            target=locked,
            before=snapshot(locked, VALUE_FIELDS),
            after=None,
        )
        # RatingState.delete() refuses; the base Model.delete is this
        # function's own path.
        models.Model.delete(locked)
    return history


class LiveUpdateFailed(Exception):
    """A live update task failed in a worker, without the error's text.

    Holds only the Match's row id and the error's class, so Celery's
    worker log never prints the original text, which can quote values.
    """

    def __init__(self, match_id: int, error_class: str) -> None:
        super().__init__(f"Live update failed for match {match_id} ({error_class}).")
        self.match_id = match_id
        self.error_class = error_class


class LiveUpdateOutcome(StrEnum):
    """What ``update_ratings_for_match()`` did."""

    UPDATED = "updated"
    UNCHANGED = "unchanged"
    MISSING = "missing"
    NOT_COUNTED = "not_counted"
    REJECTED = "rejected"
    LATE = "late"
    REPLAY_NEEDED = "replay_needed"


class ReplayFailed(Exception):
    """A replay task failed in a worker, without the error's text.

    Holds only the error's class, so Celery's worker log never prints the
    original text, which can quote values.
    """

    def __init__(self, error_class: str) -> None:
        super().__init__(f"Replay failed ({error_class}).")
        self.error_class = error_class


def request_replay(earliest_date: date, reason: ReplayReason) -> RatingReplayRequest:
    """Store a replay request and queue the replay task once the transaction commits.

    The request row is inserted in the caller's transaction, so a change
    that rolls back leaves no request and queues nothing. After commit,
    ``apps.ratings.tasks.replay_ratings`` is queued with no arguments and
    a countdown of ``settings.RATING_REPLAY_DELAY_SECONDS``, so quick
    successive changes share one replay. It locks nothing: the workflow
    calls it through ``match_changed`` while it holds Match locks. A
    broker error (an outage) is logged by error class only and does not
    fail the change: the request stays, and the beat entry runs it. Any
    other error propagates, so an eager task's failure in tests is not
    hidden.
    """
    from apps.ratings.tasks import replay_ratings as replay_task

    request = RatingReplayRequest(earliest_date=earliest_date, reason=reason)
    request.full_clean()
    request.save()

    def enqueue() -> None:
        try:
            replay_task.apply_async(countdown=settings.RATING_REPLAY_DELAY_SECONDS)
        except _BROKER_ERRORS as exc:
            logger.error("Could not queue the replay (%s).", type(exc).__name__)

    # Not robust: a robust callback would swallow any other error and log
    # its text.
    transaction.on_commit(enqueue)
    return request


def schedule_replay(
    players: Iterable[Player],
    earliest_date: date,
    *,
    reason: ReplayReason = ReplayReason.LATE,
) -> None:
    """Hand a match's players to a replay from ``earliest_date``.

    Where a live update sends a late match or one the stored snapshot
    cannot continue. Logs the players' OPEN Codes and the date, never a
    name or other personal data, then stores a replay request through
    ``request_replay()`` with ``reason``. The live update passes no
    reason, so its requests carry ``late``. Every replay is a full
    replay; the players and date are for the log only.
    """
    codes = sorted(str(player.public_code) for player in players)
    logger.info(
        "Live update skipped, replay needed from %s for players %s",
        earliest_date.isoformat(),
        ", ".join(codes),
    )
    request_replay(earliest_date, reason)


def _snapshot_row(labels: LiveConfig) -> EngineSnapshot:
    """The engine snapshot row for ``labels``, created if new, locked.

    A first run racing another first run: ``get_or_create`` catches the
    unique violation and reads the other's row. Both then lock it.
    """
    row, _ = EngineSnapshot.objects.get_or_create(
        parameter_fingerprint=labels.parameter_fingerprint,
        defaults={
            "config_fingerprint": labels.config_fingerprint,
            "config_version": labels.config_version,
            "params_version": labels.params_version,
            "engine_version": engine_version(),
            "snapshot": {},
        },
    )
    locked: EngineSnapshot = EngineSnapshot.objects.select_for_update().get(pk=row.pk)
    return locked


def _save_engine_snapshot(row: EngineSnapshot, source: LiveUpdate | ReplayRun) -> None:
    """Store ``source``'s snapshot and config labels on the locked ``row``.

    The one writer of a snapshot, for the live update, the replay, and the
    recompute.
    """
    row.snapshot = source.snapshot
    row.config_fingerprint = source.config_fingerprint
    row.config_version = source.config_version
    row.params_version = source.params_version
    row.engine_version = engine_version()
    row.full_clean()
    row.save()


def _rating_formats(match: Match) -> list[str]:
    """The formats a Match can update: its own, and overall doubles for doubles."""
    formats = [str(match.rating_format)]
    if (
        match.format == MatchFormat.DOUBLES
        and match.rating_format != RatingFormat.DOUBLES
    ):
        formats.append(str(RatingFormat.DOUBLES))
    return formats


def _share_lock_match(match_id: int) -> None:
    """Lock the Match row ``FOR SHARE`` until the transaction ends.

    Django's ``select_for_update`` has no share mode, so this is raw SQL
    on the model's table, with the id as a parameter.
    """
    # The table name is the model's own, never input.
    table = connection.ops.quote_name(Match._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT 1 FROM {table} WHERE id = %s FOR SHARE",
            [match_id],
        )


def _corrected_matches(match: Match) -> list[Match]:
    """The Matches ``match`` corrects, directly or through earlier corrections."""
    chain: list[Match] = []
    seen = {match.pk}
    current = match
    while current.corrects_id is not None and current.corrects_id not in seen:
        seen.add(current.corrects_id)
        current = Match.objects.get(pk=current.corrects_id)
        chain.append(current)
    return chain


def _record_and_apply(
    row: EngineSnapshot, match: Match, players: list[Player]
) -> LiveUpdateOutcome:
    """Record ``match`` on the locked snapshot and apply the changes.

    Runs inside ``update_ratings_for_match()``'s transaction, after its
    locks and checks.
    """
    update = record_live(row.snapshot or None, match)
    if update.already_recorded:
        return LiveUpdateOutcome.UNCHANGED
    if not update.counted:
        logger.info(
            "Live update rejected by the engine for match %s: %s",
            match.public_code,
            update.reason,
        )
        return LiveUpdateOutcome.REJECTED
    if update.late:
        schedule_replay(players, earliest_date=match.played_on)
        return LiveUpdateOutcome.LATE

    by_code = {str(player.public_code): player for player in players}
    for summary, delta in sorted(
        update.summaries,
        key=lambda item: (by_code[item[0].player_id].pk, item[0].rating_format),
    ):
        apply_rating(
            player=by_code[summary.player_id],
            summary=summary,
            kind=RatingChangeKind.LIVE_UPDATE,
            match=match,
            delta=delta,
        )
    _save_engine_snapshot(row, update)
    return LiveUpdateOutcome.UPDATED


def update_ratings_for_match(match_id: int) -> LiveUpdateOutcome:
    """Apply the live rating update for the Match with row id ``match_id``.

    In one transaction: lock the engine snapshot row for the loaded config
    (every live update serializes on it), then the Match's Players in row
    id order, then the Match row ``FOR SHARE`` (so a void, dispute, or
    edit waits until this update commits), check again that it counts,
    then record the Match on the engine the snapshot holds,
    apply each changed rating through ``apply_rating()``, and save the new
    snapshot. A failure anywhere rolls back the ratings and the snapshot
    together, so the task can run again.

    Writes nothing for a Match that does not exist (``missing``), that no
    longer counts (``not_counted``), that the stored snapshot already holds
    (``unchanged``: a redelivery, or a Match a replay counted; checked
    before the checks below), or that the engine does not count
    (``rejected``; the snapshot is not saved, so its id stays free).

    The cases below change no rating or snapshot; each stores a replay
    request through ``schedule_replay()``. A Match rated live that the
    snapshot no longer holds (a replay dropped it, and it counts again)
    returns ``replay_needed``. A Match earlier than a player's stored last
    match in a format it updates, or one the engine finds late, returns
    ``late``. A participant whose stored rating in a format the Match
    updates names another parameter fingerprint, or exists while the
    snapshot is empty, would restart from an empty engine, and a stored
    snapshot whose config fingerprint or version differs from the loaded
    config's cannot be loaded: both return ``replay_needed``. So does a
    correction of a Match that was rated live (any Match in its
    ``corrects`` chain has a live update history row): every player in the
    chain goes to the replay, from the chain's earliest date. Any other
    error (``EngineMappingError``, the engine's errors, an apply failure)
    is logged with the Match id and error class only, then propagates.
    """
    with transaction.atomic():
        match = Match.objects.filter(pk=match_id).first()
        if match is None:
            return LiveUpdateOutcome.MISSING
        if not Match.objects.counted().filter(pk=match.pk).exists():
            return LiveUpdateOutcome.NOT_COUNTED
        labels = live_config()
        row = _snapshot_row(labels)
        # Lock order: the snapshot row, then the Players by row id. A
        # subquery, not a join, so only the Player rows are locked.
        players = list(
            Player.objects.select_for_update(no_key=True)
            .filter(
                pk__in=MatchParticipant.objects.filter(match=match).values("player_id")
            )
            .order_by("pk")
        )
        # Hold the Match row until commit: the workflow's void, dispute,
        # and edit lock it FOR UPDATE, so they wait for this update.
        _share_lock_match(match.pk)
        # Again under the locks: the Match may have been voided or held
        # while this update waited for the snapshot row.
        if not Match.objects.counted().filter(pk=match.pk).exists():
            return LiveUpdateOutcome.NOT_COUNTED
        # A Match the stored snapshot already holds is in the ratings: a
        # redelivery, or a Match a replay counted. Checked before the
        # history, correction, and late checks, so neither asks for
        # another replay.
        if snapshot_holds_match(row.snapshot, match):
            return LiveUpdateOutcome.UNCHANGED
        # A Match rated live that the snapshot no longer holds: a replay
        # dropped it (a dispute, or a hold) and it counts again. Only a
        # replay can put it back.
        if RatingHistory.objects.filter(
            match=match, kind=RatingChangeKind.LIVE_UPDATE
        ).exists():
            schedule_replay(players, earliest_date=match.played_on)
            return LiveUpdateOutcome.REPLAY_NEEDED
        # A correction of a Match that was rated live: the old Match's
        # effect is still in the ratings and the snapshot, so only a replay
        # can put the correction in its place.
        chain = _corrected_matches(match)
        if RatingHistory.objects.filter(
            match__in=chain, kind=RatingChangeKind.LIVE_UPDATE
        ).exists():
            schedule_replay(
                Player.objects.filter(
                    pk__in=MatchParticipant.objects.filter(
                        match__in=[match, *chain]
                    ).values("player_id")
                ).order_by("pk"),
                earliest_date=min(m.played_on for m in [match, *chain]),
            )
            return LiveUpdateOutcome.REPLAY_NEEDED
        # A snapshot taken under another config (an engine upgrade) cannot
        # be loaded; a brand-new empty row has nothing to load.
        if row.snapshot and (
            row.config_fingerprint != labels.config_fingerprint
            or row.config_version != labels.config_version
        ):
            schedule_replay(players, earliest_date=match.played_on)
            return LiveUpdateOutcome.REPLAY_NEEDED
        formats = _rating_formats(match)
        states = RatingState.objects.filter(
            player__in=players, rating_format__in=formats
        )
        # A rating from another parameter set, or one the snapshot does not
        # hold, would restart from an empty engine: only a replay can
        # continue it.
        if states.exclude(
            parameter_fingerprint=labels.parameter_fingerprint
        ).exists() or (not row.snapshot and states.exists()):
            schedule_replay(players, earliest_date=match.played_on)
            return LiveUpdateOutcome.REPLAY_NEEDED
        if RatingState.objects.filter(
            player__in=players,
            rating_format__in=formats,
            last_played__gt=match.played_on,
        ).exists():
            schedule_replay(players, earliest_date=match.played_on)
            return LiveUpdateOutcome.LATE

        try:
            return _record_and_apply(row, match, players)
        # Any error: log the Match id and the class only (the text can quote
        # values), then let the task fail; the transaction rolls back.
        except Exception as exc:
            logger.error(
                "Live update failed for match %s (%s).",
                match.pk,
                type(exc).__name__,
            )
            raise


# Full replays (#47).


class ReplayOutcome(StrEnum):
    """What ``replay_ratings()`` did."""

    REPLAYED = "replayed"
    NOTHING_REQUESTED = "nothing_requested"


def replay_as_of() -> date:
    """The date a replay runs as of.

    Today's date in UTC, or the latest ``played_on`` among the matches
    that count when that is later: the engine skips a match dated after
    ``as_of``, and ``validate_match`` allows the latest local date
    anywhere on Earth (up to UTC+14), so no counted match is left out.
    """
    today = timezone.now().date()
    latest = Match.objects.counted().aggregate(latest=Max("played_on"))["latest"]
    return today if latest is None or latest <= today else latest


class RecomputeChangeKind(StrEnum):
    """What a full replay did, or would do, to one stored rating."""

    CREATED = "created"
    CHANGED = "changed"
    REMOVED = "removed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class RecomputeChange:
    """One rating a full replay created, changed, removed, or left alone.

    ``player`` is the Player's OPEN Code. ``before`` and ``after`` are the
    stored and replayed ``rating`` (None where there is none): the
    engine's values, never computed here (hard rule 2).
    """

    player: str
    rating_format: str
    kind: RecomputeChangeKind
    before: float | None
    after: float | None


def _change(
    player: Player,
    rating_format: str,
    stored: RatingState | None,
    summary: RatingSummary | None,
    kind: RecomputeChangeKind,
) -> RecomputeChange:
    return RecomputeChange(
        player=str(player.public_code),
        rating_format=rating_format,
        kind=kind,
        before=None if stored is None else stored.rating,
        after=None if summary is None else summary.rating,
    )


def _replayed_by_player(run: ReplayRun) -> dict[str, dict[str, RatingSummary]]:
    summaries: dict[str, dict[str, RatingSummary]] = {}
    for summary in run.summaries:
        summaries.setdefault(summary.player_id, {})[summary.rating_format] = summary
    return summaries


def _apply_replay(run: ReplayRun, replay: RatingReplay) -> list[RecomputeChange]:
    """Store every replayed rating and remove every rating the replay dropped.

    Locks every Player the replay rated or who has a stored rating, in one
    query ordered by row id, then goes through them in that order and
    their formats in sorted order. Runs inside the caller's transaction.
    Returns what happened to each rating, in that order.
    """
    codes = {summary.player_id for summary in run.summaries}
    players = list(
        Player.objects.select_for_update(no_key=True)
        .filter(
            models.Q(public_code__in=codes)
            | models.Q(pk__in=RatingState.objects.values("player_id"))
        )
        .order_by("pk")
    )
    if codes - {str(player.public_code) for player in players}:
        raise RatingApplyError("A replayed player has no player record.")
    summaries = _replayed_by_player(run)
    changes: list[RecomputeChange] = []
    for player in players:
        replayed = summaries.get(str(player.public_code), {})
        stored = {
            state.rating_format: state
            for state in RatingState.objects.filter(player=player)
        }
        for rating_format in sorted({*replayed, *stored}):
            before = stored.get(rating_format)
            if rating_format in replayed:
                summary = replayed[rating_format]
                history = apply_rating(
                    player=player,
                    summary=summary,
                    kind=RatingChangeKind.REPLAY,
                    replay=replay,
                )
                if history is None:
                    kind = RecomputeChangeKind.UNCHANGED
                elif before is None:
                    kind = RecomputeChangeKind.CREATED
                else:
                    kind = RecomputeChangeKind.CHANGED
                changes.append(_change(player, rating_format, before, summary, kind))
            else:
                _remove_rating(
                    player=player, state=stored[rating_format], replay=replay
                )
                changes.append(
                    _change(
                        player, rating_format, before, None, RecomputeChangeKind.REMOVED
                    )
                )
    return changes


def _planned_changes(run: ReplayRun, spec_version: str) -> list[RecomputeChange]:
    """What ``_apply_replay()`` would do with ``run``, without any write or lock.

    A replayed rating is unchanged when every value ``apply_rating()``
    would store (the summary, the replay's engine version, and
    ``spec_version``) equals the stored one, exactly as ``apply_rating()``
    decides. The same order as ``_apply_replay()``.
    """
    summaries = _replayed_by_player(run)
    players = list(
        Player.objects.filter(
            models.Q(public_code__in=set(summaries))
            | models.Q(pk__in=RatingState.objects.values("player_id"))
        ).order_by("pk")
    )
    if set(summaries) - {str(player.public_code) for player in players}:
        raise RatingApplyError("A replayed player has no player record.")
    changes: list[RecomputeChange] = []
    for player in players:
        replayed = summaries.get(str(player.public_code), {})
        stored = {
            state.rating_format: state
            for state in RatingState.objects.filter(player=player)
        }
        for rating_format in sorted({*replayed, *stored}):
            before = stored.get(rating_format)
            summary = replayed.get(rating_format)
            if summary is None:
                kind = RecomputeChangeKind.REMOVED
            elif before is None:
                kind = RecomputeChangeKind.CREATED
            else:
                values: dict[str, Any] = {
                    name: getattr(summary, name) for name in SUMMARY_FIELDS
                }
                values["engine_version"] = run.provenance.engine_version
                values["spec_version"] = spec_version
                kind = (
                    RecomputeChangeKind.UNCHANGED
                    if all(
                        getattr(before, name) == values[name] for name in VALUE_FIELDS
                    )
                    else RecomputeChangeKind.CHANGED
                )
            changes.append(_change(player, rating_format, before, summary, kind))
    return changes


@dataclass(frozen=True)
class _ReplayWrite:
    run: ReplayRun
    requests: list[RatingReplayRequest]
    changes: list[RecomputeChange]


def _replay_and_write(
    row: EngineSnapshot,
    requests: list[RatingReplayRequest],
    replay_date: date,
    spec_version: str | None,
) -> _ReplayWrite:
    """Replay the counted ledger and store the result, under ``row``'s lock.

    The shared body of ``replay_ratings()`` and ``recompute_ratings()``:
    runs the replay, stores its provenance, applies and removes ratings,
    saves the snapshot on ``row``, and deletes exactly ``requests``. The
    caller holds the transaction and has locked ``row`` first.
    """
    run = run_replay(counted_engine_matches(), replay_date)
    replay = record_replay(run.provenance, spec_version=spec_version)
    changes = _apply_replay(run, replay)
    _save_engine_snapshot(row, run)
    RatingReplayRequest.objects.filter(
        pk__in=[request.pk for request in requests]
    ).delete()
    return _ReplayWrite(run=run, requests=requests, changes=changes)


def replay_ratings(*, as_of: date | None = None) -> ReplayOutcome:
    """Recalculate every rating with a full replay of the counted ledger.

    In one transaction: lock the engine snapshot row for the loaded
    config first (the live update takes it first too, so the two
    serialize), take the pending replay requests, replay every match in
    ``Match.objects.counted()`` as of ``as_of`` (default
    ``replay_as_of()``), store the replay's provenance (hard rule 4),
    apply each replayed rating through ``apply_rating()`` and remove each
    stored rating the replay no longer produces, save the replayed
    engine's snapshot on the row, and delete exactly the requests it took.
    The requests are read after the lock and before the ledger, so every
    change whose request was taken is in the ledger read. A request
    stored while this runs stays, with its own task.

    With no request pending it writes nothing and takes no lock
    (``nothing_requested``), so quick successive changes, a redelivered
    task, and the beat entry share one replay.
    Any error is logged by class only (never its text), then propagates:
    everything rolls back and the requests stay pending.
    """
    # A plain check first, so a run with nothing to do (most beat runs)
    # takes no lock and creates no snapshot row.
    if not RatingReplayRequest.objects.exists():
        return ReplayOutcome.NOTHING_REQUESTED
    with transaction.atomic():
        labels = live_config()
        row = _snapshot_row(labels)
        requests = list(RatingReplayRequest.objects.order_by("pk"))
        if not requests:
            # Another run took them while this one waited for the lock.
            # Undo the empty snapshot row _snapshot_row may have created.
            transaction.set_rollback(True)
            return ReplayOutcome.NOTHING_REQUESTED
        try:
            replay_date = replay_as_of() if as_of is None else as_of
            written = _replay_and_write(row, requests, replay_date, None)
        # Any error: log the class only (the text can quote values), then
        # let it propagate; the transaction rolls back.
        except Exception as exc:
            logger.error("Replay failed (%s).", type(exc).__name__)
            raise
        logger.info(
            "Replay as of %s for %s requests from %s: %s matches considered, "
            "%s counted.",
            replay_date.isoformat(),
            len(requests),
            min(request.earliest_date for request in requests).isoformat(),
            written.run.provenance.matches_considered,
            written.run.provenance.matches_counted,
        )
    return ReplayOutcome.REPLAYED


# The recompute (#48, ADR 0058).


class RecomputeRefused(ValueError):
    """``recompute_ratings()`` refused its options. The message is fixed text."""


@dataclass(frozen=True)
class RecomputeResult:
    """What ``recompute_ratings()`` did, or would do in a dry run.

    ``changes`` covers every stored or replayed rating, in the order the
    replay applies them, narrowed to ``rating_format`` when one was given
    (dry run only). ``requests_cleared`` is the number of pending replay
    requests the recompute deleted (always 0 in a dry run).
    """

    as_of: date
    spec_version: str
    dry_run: bool
    rating_format: str | None
    matches_considered: int
    matches_counted: int
    requests_cleared: int
    changes: tuple[RecomputeChange, ...]

    def count(self, kind: RecomputeChangeKind) -> int:
        """The number of ratings with change ``kind``."""
        return sum(1 for change in self.changes if change.kind == kind)


def _latest_counted_played_on() -> date | None:
    latest: date | None = Match.objects.counted().aggregate(latest=Max("played_on"))[
        "latest"
    ]
    return latest


def recompute_ratings(
    *,
    as_of: date | None = None,
    rating_format: str | None = None,
    spec_version: str | None = None,
    dry_run: bool = False,
) -> RecomputeResult:
    """Rebuild every rating from the whole counted ledger (#48, ADR 0058).

    A full replay of ``Match.objects.counted()`` through the engine, as of
    ``as_of`` (default ``replay_as_of()``), whether or not a replay request
    is pending. Run it after an engine upgrade or a bump of
    ``OPEN_RATING_SPEC_VERSION`` (``docs/engine.md``).

    A write runs exactly like ``replay_ratings()``: one transaction that
    locks the engine snapshot row first, stores the replay's provenance
    with ``spec_version`` (hard rule 4), applies and removes ratings
    through ``apply_rating()`` and ``_remove_rating()``, saves the
    snapshot, and deletes the replay requests that existed when it took
    the lock (it covers them; later ones stay). Running it again writes a
    new ``RatingReplay`` but no rating change when nothing changed.

    ``dry_run`` replays and compares with the stored ratings, and writes
    nothing and takes no lock. It is a preview, not one consistent read: a
    live update that commits while it runs can show up in part of it.
    Only a dry run takes ``rating_format`` (which narrows the result), an ``as_of`` before the latest counted
    ``played_on`` (later matches would drop out of the stored ratings),
    an ``as_of`` after ``replay_as_of()`` (every rating, and every later
    live update, would be taken as of that future date),
    or a ``spec_version`` other than ``OPEN_RATING_SPEC_VERSION`` (the next
    replay would put the setting's back). Raises ``RecomputeRefused``
    (fixed text) for those, and for an unknown format, before any write.
    Any other error is logged by class only and propagates; a write rolls
    back.
    """
    setting = settings.OPEN_RATING_SPEC_VERSION
    spec = setting if spec_version is None else spec_version
    if rating_format is not None and rating_format not in RatingFormat.values:
        raise RecomputeRefused("Unknown rating format.")
    if not dry_run:
        if rating_format is not None:
            raise RecomputeRefused("A rating format is allowed only in a dry run.")
        if spec != setting:
            raise RecomputeRefused(
                "A spec version other than OPEN_RATING_SPEC_VERSION is allowed "
                "only in a dry run."
            )
    try:
        if dry_run:
            replay_date = replay_as_of() if as_of is None else as_of
            run = run_replay(counted_engine_matches(), replay_date)
            changes = _planned_changes(run, spec)
            requests_cleared = 0
        else:
            with transaction.atomic():
                row = _snapshot_row(live_config())
                requests = list(RatingReplayRequest.objects.order_by("pk"))
                latest = _latest_counted_played_on()
                if as_of is not None and latest is not None and as_of < latest:
                    raise RecomputeRefused(
                        "An as-of date before the latest counted match is "
                        "allowed only in a dry run."
                    )
                default_date = replay_as_of()
                if as_of is not None and as_of > default_date:
                    raise RecomputeRefused(
                        "An as-of date after the replay date is allowed only "
                        "in a dry run."
                    )
                replay_date = default_date if as_of is None else as_of
                written = _replay_and_write(row, requests, replay_date, spec)
                run = written.run
                changes = written.changes
                requests_cleared = len(requests)
    except RecomputeRefused:
        raise
    # Any error: log the class only (the text can quote values), then let
    # it propagate; a write rolls back.
    except Exception as exc:
        logger.error("Recompute failed (%s).", type(exc).__name__)
        raise
    if rating_format is not None:
        changes = [c for c in changes if c.rating_format == rating_format]
    result = RecomputeResult(
        as_of=replay_date,
        spec_version=spec,
        dry_run=dry_run,
        rating_format=rating_format,
        matches_considered=run.provenance.matches_considered,
        matches_counted=run.provenance.matches_counted,
        requests_cleared=requests_cleared,
        changes=tuple(changes),
    )
    logger.info(
        "Recompute%s as of %s: %s matches considered, %s counted; %s created, "
        "%s changed, %s removed, %s unchanged; %s requests cleared.",
        " (dry run)" if dry_run else "",
        replay_date.isoformat(),
        result.matches_considered,
        result.matches_counted,
        result.count(RecomputeChangeKind.CREATED),
        result.count(RecomputeChangeKind.CHANGED),
        result.count(RecomputeChangeKind.REMOVED),
        result.count(RecomputeChangeKind.UNCHANGED),
        requests_cleared,
    )
    return result

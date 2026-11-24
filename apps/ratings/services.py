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
is the replay caller's job (#47).

``update_ratings_for_match()`` is the live update (#46, ADR 0027): it
records one confirmed Match on the stored engine snapshot and applies the
changes through ``apply_rating()``. ``schedule_replay()`` is where a late
match goes; #47 fills it in.
"""

import logging
from collections.abc import Iterable
from datetime import date
from enum import StrEnum
from typing import Any

from django.conf import settings
from django.db import connection, transaction

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
    engine_version,
    live_config,
    record_live,
)
from apps.ratings.models import (
    EngineSnapshot,
    RatingChangeKind,
    RatingHistory,
    RatingReplay,
    RatingState,
)

logger = logging.getLogger(__name__)

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
    the outermost transaction ends, so the caller chooses the transaction
    for each batch. The callers (#46, #47) own ordering, and the lock
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
            },
        )
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


def schedule_replay(players: Iterable[Player], earliest_date: date) -> None:
    """Hand a late match's players to a replay from ``earliest_date``.

    A stub until #47 replaces it: it only logs the players' OPEN Codes and
    the date, never a name or other personal data.
    """
    codes = sorted(str(player.public_code) for player in players)
    logger.info(
        "Live update skipped, replay needed from %s for players %s",
        earliest_date.isoformat(),
        ", ".join(codes),
    )


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


def _save_engine_snapshot(row: EngineSnapshot, update: LiveUpdate) -> None:
    """Store ``update``'s snapshot on the locked ``row``: its only writer."""
    row.snapshot = update.snapshot
    row.config_fingerprint = update.config_fingerprint
    row.config_version = update.config_version
    row.params_version = update.params_version
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
    longer counts (``not_counted``), that the snapshot already holds
    (``unchanged``, a redelivery), or that the engine does not count
    (``rejected``; the snapshot is not saved, so its id stays free). A
    Match earlier than a player's stored last match in a format it
    updates, or one the engine finds late, writes nothing and goes to
    ``schedule_replay()`` (``late``). A participant whose stored rating in
    a format the Match updates names another parameter fingerprint, or
    exists while the snapshot is empty, would restart from an empty
    engine, and a stored snapshot whose config fingerprint or version
    differs from the loaded config's cannot be loaded: both write nothing
    and go to ``schedule_replay()`` (``replay_needed``). So does a
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
        # A counted live update always writes history rows in the same
        # transaction as the snapshot, so a Match with one is a
        # redelivery, even after a later match moved last_played.
        if RatingHistory.objects.filter(
            match=match, kind=RatingChangeKind.LIVE_UPDATE
        ).exists():
            return LiveUpdateOutcome.UNCHANGED
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

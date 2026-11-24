"""Store rating changes and replays (#45, ADR 0025).

``apply_rating()`` is the only way to change a ``RatingState``, and with
``record_replay()`` the only way to write ``RatingHistory`` and
``RatingReplay`` rows. Rating changes are a security-sensitive area (hard
rule 8).

The values stored are the engine's (``engine_adapter.RatingSummary``):
nothing here computes or rounds a rating (hard rule 2). Every change
records one ``rating.change`` Audit Event in the same transaction, with no
actor (the system). Applying the values already stored changes nothing,
so a repeated task is harmless.
"""

from typing import Any

from django.conf import settings
from django.db import transaction

from apps.audit.models import AuditAction
from apps.audit.services import record_event, snapshot
from apps.matches.models import (
    Match,
    MatchFormat,
    MatchParticipant,
    RatingFormat,
)
from apps.players.models import Player
from apps.ratings.engine_adapter import RatingSummary, ReplayProvenance, engine_version
from apps.ratings.models import (
    RatingChangeKind,
    RatingHistory,
    RatingReplay,
    RatingState,
)

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
    else:
        raise RatingApplyError("The kind of change is not known.")
    if match is None:
        return
    if not MatchParticipant.objects.filter(match=match, player=player).exists():
        raise RatingApplyError("The player did not play in the match.")
    # The engine feeds a category doubles match (mixed, men's, women's)
    # into overall doubles too, so a doubles match may update either its
    # own format or overall doubles.
    allowed = {match.rating_format}
    if match.format == MatchFormat.DOUBLES:
        allowed.add(RatingFormat.DOUBLES)
    if summary.rating_format not in allowed:
        raise RatingApplyError("The match does not count in this rating format.")


def record_replay(provenance: ReplayProvenance) -> RatingReplay:
    """Store one replay's provenance (hard rule 4) and return it.

    The spec version is ``settings.OPEN_RATING_SPEC_VERSION``. No Audit
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
        net_rating_created=dict(provenance.net_rating_created),
        engine_version=provenance.engine_version,
        spec_version=settings.OPEN_RATING_SPEC_VERSION,
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
    and returns the history row. When every value equals the stored state,
    it writes nothing and returns None.

    Raises ``RatingApplyError`` (fixed text) for input that names the wrong
    player, format, or cause, before any write, and ``ValidationError`` for
    a value the models refuse, before any write.

    The state's row lock lasts until the outermost transaction ends, so the
    caller chooses the transaction for each batch. The callers (#46, #47)
    own ordering and concurrency across players. Two first applies for the
    same player and format at once: one fails on the unique constraint.
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

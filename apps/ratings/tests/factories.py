"""Builders for rating state, rating history, and replay tests. Made-up data only.

``rating_summary`` and ``replay_provenance`` build valid plain engine
results without running the engine. ``state_values`` gives a valid set of
the value fields ``RatingState`` and ``RatingHistory`` share.
``make_state``, ``make_replay``, and ``make_history`` insert rows directly
for model tests; services tests go through ``apps.ratings.services``.
"""

from datetime import date
from typing import Any

from apps.matches.models import RatingFormat
from apps.players.models import Player
from apps.ratings.engine_adapter import RatingSummary, ReplayProvenance
from apps.ratings.models import (
    RatingChangeKind,
    RatingHistory,
    RatingReplay,
    RatingState,
)

MADE_UP_ENGINE_VERSION = "9.9.9"
MADE_UP_SPEC_VERSION = "spec-test"
MADE_UP_FINGERPRINT = "0123456789abcdef"


def rating_summary(player: Player, **overrides: Any) -> RatingSummary:
    """A valid ``RatingSummary`` for ``player`` in overall doubles."""
    values: dict[str, Any] = {
        "player_id": str(player.public_code),
        "rating_format": str(RatingFormat.DOUBLES.value),
        "rating": 3.512,
        "rd": 0.4123,
        "reliability": 41.5,
        "imported_reliability": None,
        "established": False,
        "matches_played": 3,
        "last_played": date(2026, 3, 1),
        "calculated_as_of": None,
        "live_updates_since_recalculation": 0,
        "config_version": "1.0.0",
        "params_version": "v1",
        "parameter_fingerprint": MADE_UP_FINGERPRINT,
    }
    values.update(overrides)
    return RatingSummary(**values)


def replay_provenance(**overrides: Any) -> ReplayProvenance:
    """A valid ``ReplayProvenance``."""
    values: dict[str, Any] = {
        "as_of": date(2026, 3, 31),
        "config_version": "1.0.0",
        "config_fingerprint": "fedcba9876543210",
        "params_version": "v1",
        "parameter_fingerprint": MADE_UP_FINGERPRINT,
        "matches_considered": 12,
        "matches_counted": 10,
        "net_rating_created": {"singles": 0.0, "doubles": 0.25},
        "engine_version": MADE_UP_ENGINE_VERSION,
    }
    values.update(overrides)
    return ReplayProvenance(**values)


def state_values(**overrides: Any) -> dict[str, Any]:
    """Valid values for the fields ``RatingState`` and ``RatingHistory`` share."""
    values: dict[str, Any] = {
        "rating_format": RatingFormat.DOUBLES,
        "rating": 3.512,
        "rd": 0.4123,
        "reliability": 41.5,
        "imported_reliability": None,
        "established": False,
        "matches_played": 3,
        "last_played": date(2026, 3, 1),
        "calculated_as_of": None,
        "live_updates_since_recalculation": 0,
        "config_version": "1.0.0",
        "params_version": "v1",
        "parameter_fingerprint": MADE_UP_FINGERPRINT,
        "engine_version": MADE_UP_ENGINE_VERSION,
        "spec_version": MADE_UP_SPEC_VERSION,
    }
    values.update(overrides)
    return values


def replay_values(**overrides: Any) -> dict[str, Any]:
    """Valid field values for a ``RatingReplay``."""
    values: dict[str, Any] = {
        "as_of": date(2026, 3, 31),
        "config_version": "1.0.0",
        "config_fingerprint": "fedcba9876543210",
        "params_version": "v1",
        "parameter_fingerprint": MADE_UP_FINGERPRINT,
        "matches_considered": 12,
        "matches_counted": 10,
        "net_rating_created": {"doubles": 0.25},
        "engine_version": MADE_UP_ENGINE_VERSION,
        "spec_version": MADE_UP_SPEC_VERSION,
    }
    values.update(overrides)
    return values


def make_state(player: Player, **overrides: Any) -> RatingState:
    """Insert a ``RatingState`` row directly (model tests only)."""
    state: RatingState = RatingState.objects.create(
        player=player, **state_values(**overrides)
    )
    return state


def make_replay(**overrides: Any) -> RatingReplay:
    """Insert a ``RatingReplay`` row directly (model tests only)."""
    replay: RatingReplay = RatingReplay.objects.create(**replay_values(**overrides))
    return replay


def make_history(player: Player, **overrides: Any) -> RatingHistory:
    """Insert a replay ``RatingHistory`` row directly (model tests only).

    Without ``kind``, ``replay``, or ``match``, it is a replay row of a new
    replay.
    """
    values = state_values()
    values["kind"] = RatingChangeKind.REPLAY
    values.update(overrides)
    if values["kind"] == RatingChangeKind.REPLAY and "replay" not in values:
        values["replay"] = make_replay()
    history: RatingHistory = RatingHistory.objects.create(player=player, **values)
    return history

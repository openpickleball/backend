"""A player's summary from the engine as plain data (#45).

``rating_summary`` turns an ``open_engine.PlayerSummary`` into a frozen
``RatingSummary`` of plain values, as the engine returned them (hard rule
2), with imported reliability kept apart from OPEN-earned reliability
(hard rule 5). Made-up data only.
"""

import dataclasses
from datetime import date

import open_engine
import pytest

from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Team
from apps.players.models import GenderDivision
from apps.ratings.engine_adapter import (
    RatingSummary,
    load_config,
    rating_summary,
    to_engine_match,
)
from apps.ratings.tests.builders import saved_match

PLAIN_LEAVES = (str, int, float, bool, type(None), date)


def _assert_plain(summary: RatingSummary) -> None:
    for field in dataclasses.fields(summary):
        value = getattr(summary, field.name)
        assert type(value) in PLAIN_LEAVES, (field.name, type(value))


@pytest.mark.django_db
def test_a_played_summary_converts_to_plain_data() -> None:
    match, players = saved_match(
        event=EventFactory.create(kind=EventKind.TOURNAMENT),
        divisions={
            Team.A: (GenderDivision.MENS, GenderDivision.MENS),
            Team.B: (GenderDivision.MENS, GenderDivision.MENS),
        },
    )
    engine = open_engine.RatingEngine(load_config())
    engine.record(to_engine_match(match))
    code = str(players[Team.A][0].public_code)
    engine_summary = engine.player_summary(
        code, open_engine.RatingFormat.DOUBLES_MEN, as_of=date(2026, 3, 31)
    )

    summary = rating_summary(engine_summary)

    _assert_plain(summary)
    assert summary.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert summary.player_id == code
    assert summary.rating_format == "doubles_men"
    assert type(summary.rating_format) is str
    assert summary.rating == engine_summary.rating
    assert summary.rd == engine_summary.rd
    assert summary.reliability == engine_summary.reliability
    assert summary.imported_reliability is None
    assert summary.established is engine_summary.established
    assert summary.matches_played == engine_summary.matches_played == 1
    assert summary.last_played == engine_summary.last_played == match.played_on
    assert summary.calculated_as_of is None
    assert summary.live_updates_since_recalculation == (
        engine_summary.live_updates_since_recalculation
    )
    assert summary.config_version == engine_summary.config_version
    assert summary.params_version == engine_summary.params_version == "v1"
    assert summary.parameter_fingerprint == engine_summary.parameter_fingerprint


def test_imported_reliability_is_carried_apart() -> None:
    engine = open_engine.RatingEngine(load_config())
    engine.seed_external_rating(
        "made-up-player",
        open_engine.RatingFormat.SINGLES,
        open_engine.ExternalRatingSnapshot(
            source="external",
            rating=3.5,
            captured_on=date(2026, 1, 15),
            reliability=72.0,
        ),
    )
    engine_summary = engine.player_summary(
        "made-up-player", open_engine.RatingFormat.SINGLES, as_of=date(2026, 2, 1)
    )
    assert engine_summary.imported_reliability is not None

    summary = rating_summary(engine_summary)

    _assert_plain(summary)
    assert summary.imported_reliability == engine_summary.imported_reliability
    assert summary.reliability == engine_summary.reliability
    assert summary.matches_played == 0
    assert summary.last_played is None

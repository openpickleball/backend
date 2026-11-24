"""Recording one match live against a stored engine snapshot (#46, ADR 0027).

``record_live`` rebuilds the engine from a snapshot (or starts an empty
one), records one Match without ``as_of``, and returns plain data: each
counted format's summaries with the engine's delta, whether the match was
late, and the new snapshot. Made-up data only.
"""

import json
from datetime import timedelta

import open_engine
import pytest

from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import MatchFormat, Team
from apps.players.models import GenderDivision
from apps.ratings.engine_adapter import (
    live_config,
    load_config,
    record_live,
    to_engine_match,
)
from apps.ratings.tests.builders import MIXED, PLAYED_ON, saved_match

pytestmark = pytest.mark.django_db

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _tournament() -> Event:
    event: Event = EventFactory.create(kind=EventKind.TOURNAMENT)
    return event


def test_live_config_names_the_loaded_config() -> None:
    cfg = load_config()
    identity = live_config()
    assert identity.parameter_fingerprint == cfg.parameter_fingerprint()
    assert identity.config_fingerprint == cfg.fingerprint()
    assert identity.config_version == cfg.version
    assert identity.params_version == cfg.params_version


def test_a_first_match_starts_an_empty_engine() -> None:
    match, players = saved_match(
        match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES, event=_tournament()
    )

    update = record_live(None, match)

    expected = open_engine.RatingEngine(load_config()).record(to_engine_match(match))
    assert update.counted
    assert update.reason is None
    assert not update.late
    assert not update.already_recorded
    codes = sorted(str(p.public_code) for team in players.values() for p in team)
    assert [s.player_id for s, _ in update.summaries] == codes
    for summary, delta in update.summaries:
        assert summary.rating_format == "singles"
        assert delta == expected.deltas[summary.player_id]
        assert summary.matches_played == 1
        assert summary.live_updates_since_recalculation == 1
        assert summary.last_played == PLAYED_ON
        assert summary.calculated_as_of is None
    identity = live_config()
    assert update.parameter_fingerprint == identity.parameter_fingerprint
    assert update.config_fingerprint == identity.config_fingerprint
    assert update.config_version == identity.config_version
    assert update.params_version == identity.params_version


def test_the_snapshot_is_json_and_rebuilds_the_engine() -> None:
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES, event=_tournament()
    )
    update = record_live(None, match)

    text = json.dumps(update.snapshot, allow_nan=False)
    engine = open_engine.RatingEngine.from_snapshot(json.loads(text), load_config())
    assert str(match.public_code) in engine.processed_match_ids


def test_a_category_doubles_match_updates_its_format_and_overall_doubles() -> None:
    match, players = saved_match(divisions=MIXED, event=_tournament())

    update = record_live(None, match)

    expected = open_engine.RatingEngine(load_config()).record(to_engine_match(match))
    assert expected.parent is not None
    codes = sorted(str(p.public_code) for team in players.values() for p in team)
    by_format: dict[str, dict[str, float]] = {}
    for summary, delta in update.summaries:
        by_format.setdefault(summary.rating_format, {})[summary.player_id] = delta
    assert sorted(by_format) == ["doubles", "mixed_doubles"]
    assert by_format["mixed_doubles"] == dict(expected.deltas)
    assert by_format["doubles"] == dict(expected.parent.deltas)
    assert sorted(by_format["doubles"]) == codes
    keys = [(s.rating_format, s.player_id) for s, _ in update.summaries]
    assert keys == sorted(keys)


def test_a_second_match_builds_on_the_first() -> None:
    first, players = saved_match(
        match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES, event=_tournament()
    )
    second, _ = saved_match(
        match_format=MatchFormat.SINGLES,
        players=players,
        event=_tournament(),
        played_on=PLAYED_ON + timedelta(days=1),
    )
    first_update = record_live(None, first)

    update = record_live(first_update.snapshot, second)

    assert update.counted
    assert {s.matches_played for s, _ in update.summaries} == {2}
    assert {s.live_updates_since_recalculation for s, _ in update.summaries} == {2}


def test_a_match_already_in_the_snapshot_is_already_recorded() -> None:
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES, event=_tournament()
    )
    first = record_live(None, match)

    again = record_live(first.snapshot, match)

    assert again.already_recorded
    assert again.summaries == ()
    assert again.snapshot == first.snapshot


def test_an_earlier_match_after_a_later_one_is_late() -> None:
    later, players = saved_match(
        match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES, event=_tournament()
    )
    earlier, _ = saved_match(
        match_format=MatchFormat.SINGLES,
        players=players,
        event=_tournament(),
        played_on=PLAYED_ON - timedelta(days=5),
    )
    first = record_live(None, later)

    update = record_live(first.snapshot, earlier)

    assert update.late


def test_a_late_category_match_is_late_through_its_parent() -> None:
    # A later overall doubles match first, then an earlier mixed match: no
    # mixed state is later, but the overall doubles update is late.
    later, players = saved_match(
        divisions=MIXED, event=_tournament(), rating_format="doubles"
    )
    earlier, _ = saved_match(
        players=players,
        event=_tournament(),
        played_on=PLAYED_ON - timedelta(days=5),
    )
    assert earlier.rating_format == "mixed_doubles"
    first = record_live(None, later)

    update = record_live(first.snapshot, earlier)

    assert update.late


def test_a_match_the_engine_does_not_count_has_its_reason() -> None:
    # Self-reported, confirmed by one team only (ADR 0024).
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        confirm=[(Team.A, 0)],
    )

    update = record_live(None, match)

    assert not update.counted
    assert update.reason == "self_reported_unconfirmed"
    assert update.summaries == ()

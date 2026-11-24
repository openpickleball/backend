"""Whether a stored engine snapshot holds a Match (#47). Made-up data only."""

import pytest

from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, Team
from apps.players.models import GenderDivision
from apps.ratings.engine_adapter import record_live, snapshot_holds_match
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _match() -> Match:
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        event=EventFactory.create(kind=EventKind.TOURNAMENT),
    )
    return match


def test_a_snapshot_that_recorded_the_match_holds_it() -> None:
    match = _match()
    snapshot = record_live(None, match).snapshot

    assert snapshot_holds_match(snapshot, match) is True
    assert snapshot_holds_match(snapshot, _match()) is False


@pytest.mark.parametrize("snapshot", [None, {}])
def test_an_empty_snapshot_holds_nothing(snapshot: dict[str, object] | None) -> None:
    assert snapshot_holds_match(snapshot, _match()) is False


def test_a_snapshot_that_cannot_be_loaded_holds_nothing() -> None:
    match = _match()
    snapshot = dict(record_live(None, match).snapshot)
    snapshot["schema_version"] = -1

    assert snapshot_holds_match(snapshot, match) is False

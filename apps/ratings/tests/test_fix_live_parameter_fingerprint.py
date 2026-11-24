"""A live update never restarts a player rated under another parameter set (#46).

When a participant's stored rating names another parameter fingerprint
than the loaded config's, or the engine snapshot is empty while a
participant already has a rating, recording the Match on the snapshot
would rate the player from scratch and overwrite their state. The live
update writes nothing and hands the players to ``schedule_replay``
(``replay_needed``). Made-up data only.
"""

from datetime import date, timedelta

import pytest

from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, Team
from apps.players.models import GenderDivision, Player
from apps.ratings import services
from apps.ratings.engine_adapter import live_config
from apps.ratings.models import EngineSnapshot, RatingHistory, RatingState
from apps.ratings.services import update_ratings_for_match
from apps.ratings.tests.builders import PLAYED_ON, saved_match
from apps.ratings.tests.factories import make_state

pytestmark = pytest.mark.django_db

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _tournament() -> Event:
    event: Event = EventFactory.create(kind=EventKind.TOURNAMENT)
    return event


def _singles(
    players: dict[Team, list[Player]] | None = None, played_on: date = PLAYED_ON
) -> tuple[Match, dict[Team, list[Player]]]:
    return saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        players=players,
        event=_tournament(),
        played_on=played_on,
    )


def _all(players: dict[Team, list[Player]]) -> list[Player]:
    return [player for team in Team for player in players[team]]


def _record_replays(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], date]]:
    calls: list[tuple[list[str], date]] = []
    monkeypatch.setattr(
        services,
        "schedule_replay",
        lambda found, earliest_date: calls.append(
            (sorted(str(p.public_code) for p in found), earliest_date)
        ),
    )
    return calls


def test_a_state_under_another_parameter_fingerprint_needs_a_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, players = _singles()
    second, _ = _singles(players, PLAYED_ON + timedelta(days=1))
    update_ratings_for_match(first.pk)
    # The stored ratings came from another parameter set.
    for state in RatingState.objects.all():
        state.parameter_fingerprint = "made-up-older-fingerprint"
        state.save()
    history = RatingHistory.objects.count()
    calls = _record_replays(monkeypatch)

    outcome = update_ratings_for_match(second.pk)

    assert outcome == "replay_needed"
    assert RatingHistory.objects.count() == history
    assert not RatingHistory.objects.filter(match=second).exists()
    assert calls == [
        (sorted(str(p.public_code) for p in _all(players)), second.played_on)
    ]


def test_an_empty_snapshot_with_a_stored_state_needs_a_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match, players = _singles(played_on=PLAYED_ON + timedelta(days=1))
    # A rating stored under the loaded parameter set, but no snapshot.
    state = make_state(
        players[Team.A][0],
        rating_format="singles",
        matches_played=40,
        last_played=PLAYED_ON,
        parameter_fingerprint=live_config().parameter_fingerprint,
    )
    calls = _record_replays(monkeypatch)

    outcome = update_ratings_for_match(match.pk)

    assert outcome == "replay_needed"
    state.refresh_from_db()
    assert state.matches_played == 40
    assert not RatingHistory.objects.exists()
    assert not any(row.snapshot for row in EngineSnapshot.objects.all())
    assert len(calls) == 1

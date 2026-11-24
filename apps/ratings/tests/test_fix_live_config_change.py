"""A live update asks for a replay when the stored snapshot's config changed (#46).

An engine upgrade can change the config fingerprint or version while the
parameter fingerprint stays the same. The stored snapshot was taken under
the old config, so the live update writes nothing and hands the players
to ``schedule_replay`` (``replay_needed``) instead of loading it. A
brand-new empty row is not checked. Made-up data only.
"""

from datetime import date, timedelta

import pytest

from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, Team
from apps.players.models import GenderDivision, Player
from apps.ratings import services
from apps.ratings.models import EngineSnapshot, RatingHistory
from apps.ratings.services import update_ratings_for_match
from apps.ratings.tests.builders import PLAYED_ON, saved_match

pytestmark = pytest.mark.django_db

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _singles(
    players: dict[Team, list[Player]] | None = None, played_on: date = PLAYED_ON
) -> tuple[Match, dict[Team, list[Player]]]:
    event: Event = EventFactory.create(kind=EventKind.TOURNAMENT)
    return saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        players=players,
        event=event,
        played_on=played_on,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("config_fingerprint", "0123456789abcdef"), ("config_version", "0.0.1")],
)
def test_a_snapshot_under_another_config_needs_a_replay(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    first, players = _singles()
    second, _ = _singles(players, PLAYED_ON + timedelta(days=1))
    update_ratings_for_match(first.pk)
    row = EngineSnapshot.objects.get()
    setattr(row, field, value)
    row.save()
    stored = row.snapshot
    calls: list[date] = []
    monkeypatch.setattr(
        services,
        "schedule_replay",
        lambda found, earliest_date: calls.append(earliest_date),
    )

    outcome = update_ratings_for_match(second.pk)

    assert outcome == "replay_needed"
    assert not RatingHistory.objects.filter(match=second).exists()
    row.refresh_from_db()
    assert row.snapshot == stored
    assert getattr(row, field) == value
    assert calls == [second.played_on]

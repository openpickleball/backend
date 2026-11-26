"""A player's correction of a disputed self-reported match (#267).

The correction is created pending with the corrector's ``confirm`` row, so
it does not count until a player on the other team confirms it. Once they
do, ``confirmed_by`` is exactly the corrector and that player, the players
with a ``confirm`` decision. Made-up data only.
"""

from datetime import date

import open_engine
import pytest

from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.workflow import confirm_match, dispute_match, edit_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.engine_adapter import (
    counted_engine_matches,
    load_config,
    to_engine_match,
)

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 3, 1)


@pytest.fixture
def players() -> list[Player]:
    """Team A is players 0 and 1, team B players 2 and 3."""
    return [PlayerFactory.create() for _ in range(4)]


def _codes(players: list[Player]) -> frozenset[str]:
    return frozenset(str(player.public_code) for player in players)


def test_a_player_correction_of_a_disputed_match_sends_only_the_confirmers(
    players: list[Player],
) -> None:
    old = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=[(11, 4)],
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    dispute_match(old, actor=players[1].account)
    corrected = edit_match(
        Match.objects.get(pk=old.pk),
        actor=players[1].account,
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=[(11, 7)],
        played_on=PLAYED_ON,
        as_player=True,
    )
    confirm_match(corrected, actor=players[3].account)
    fresh = Match.objects.get(pk=corrected.pk)
    assert fresh.status == MatchStatus.CONFIRMED

    converted = to_engine_match(fresh)

    assert converted.confirmed_by == _codes([players[1], players[3]])
    [listed] = [m for m in counted_engine_matches() if m.match_id == fresh.public_code]
    assert listed == converted
    assert open_engine.RatingEngine(load_config()).record(converted).counted

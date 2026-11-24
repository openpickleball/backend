"""The player lock's strength and the replay's net rating shape (#45).

``apply_rating`` locks the Player row with ``FOR NO KEY UPDATE``, which
does not block inserts of rows that point at the Player (their ``FOR KEY
SHARE`` lock), such as Match Participants and Match Confirmations. A
replay's ``net_rating_created`` is always a mapping, empty or not. Made-up
data only.
"""

from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.matches.models import Game, Match, MatchParticipant, MatchStatus, Team
from apps.matches.tests.factories import MatchFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import RatingChangeKind, RatingReplay
from apps.ratings.services import apply_rating, record_replay
from apps.ratings.tests.factories import (
    rating_summary,
    replay_provenance,
    replay_values,
)

pytestmark = pytest.mark.django_db


def _counted_match(player: Player) -> Match:
    match: Match = MatchFactory.create(
        status=MatchStatus.CONFIRMED, participants=False, games=False
    )
    MatchParticipant.objects.create(match=match, player=player, team=Team.A)
    MatchParticipant.objects.create(
        match=match, player=PlayerFactory.create(), team=Team.B
    )
    Game.objects.create(match=match, game_number=1, team_a_score=11, team_b_score=3)
    return match


def test_the_player_lock_does_not_block_key_share_locks() -> None:
    player: Player = PlayerFactory.create()
    match = _counted_match(player)

    with CaptureQueriesContext(connection) as captured:
        apply_rating(
            player=player,
            summary=rating_summary(player, rating_format="singles"),
            kind=RatingChangeKind.LIVE_UPDATE,
            match=match,
        )

    player_locks = [
        query["sql"]
        for query in captured.captured_queries
        if 'FROM "players_player"' in query["sql"] and "FOR " in query["sql"]
    ]
    assert player_locks, "no lock on the player row"
    for sql in player_locks:
        assert sql.rstrip().endswith("FOR NO KEY UPDATE"), sql


NOT_MAPPINGS: list[Any] = [[], "", ()]


@pytest.mark.parametrize("value", NOT_MAPPINGS, ids=["list", "text", "tuple"])
def test_a_replay_refuses_an_empty_value_that_is_not_a_mapping(value: Any) -> None:
    replay = RatingReplay(**replay_values(net_rating_created=value))

    with pytest.raises(ValidationError) as caught:
        replay.full_clean()

    assert "net_rating_created" in caught.value.message_dict


@pytest.mark.parametrize("value", NOT_MAPPINGS, ids=["list", "text", "tuple"])
def test_record_replay_stores_nothing_for_a_value_that_is_not_a_mapping(
    value: Any,
) -> None:
    with pytest.raises(ValidationError):
        record_replay(replay_provenance(net_rating_created=value))

    assert not RatingReplay.objects.exists()

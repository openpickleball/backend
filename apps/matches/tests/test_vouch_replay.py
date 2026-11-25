"""Vouched near duplicates count after the next replay (#270). Made-up data only.

Two self-reported near duplicates are held from ratings. Once one player
of each team vouches for each, the flag is resolved, each match's
``release`` stores a replay request, and the replay rates both.
"""

import pytest

from apps.matches.models import Match, MatchFormat
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import confirm_match, vouch_for_match
from apps.players.models import Player
from apps.ratings.models import (
    RatingReplay,
    RatingReplayRequest,
    RatingState,
    ReplayReason,
)
from apps.ratings.services import ReplayOutcome, replay_ratings

pytestmark = pytest.mark.django_db


def test_vouched_near_duplicates_are_rated_by_the_next_replay(
    confirmed: Match, players: list[Player]
) -> None:
    later = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(later, actor=players[0].account)
    confirm_match(later, actor=players[2].account)
    assert not Match.objects.counted().exists()
    releases = RatingReplayRequest.objects.filter(reason=ReplayReason.RELEASE)
    assert not releases.exists()

    for match in (confirmed, later):
        vouch_for_match(match, actor=players[1].account)
        vouch_for_match(match, actor=players[3].account)

    assert releases.count() == 2
    assert replay_ratings() == ReplayOutcome.REPLAYED
    replay = RatingReplay.objects.get()
    assert replay.matches_counted == 2
    rated = set(RatingState.objects.values_list("player_id", flat=True))
    assert rated == {player.pk for player in players}

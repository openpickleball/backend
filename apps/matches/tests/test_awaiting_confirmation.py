"""Matches awaiting the signed-in player's confirmation (issue #192).

A Match is listed for a Player while it is pending, the Player played in
it, and no player on the Player's team has confirmed it. Made-up data
only.
"""

import pytest

from apps.accounts.models import User
from apps.matches.models import Match, MatchParticipant, MatchStatus, Team
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.workflow import (
    awaiting_confirmation,
    confirm_match,
    dispute_match,
    void_match,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _codes(player: Player) -> set[str]:
    return {match.public_code for match in awaiting_confirmation(player)}


# The service.


def test_pending_match_is_listed_for_every_player_in_it(
    pending: Match, players: list[Player]
) -> None:
    for player in players:
        assert _codes(player) == {pending.public_code}


def test_submitter_sees_their_own_pending_match(
    pending: Match, players: list[Player]
) -> None:
    # players[0] submitted it; a submission records no confirmation.
    assert pending.submitted_by_user == players[0].account
    assert _codes(players[0]) == {pending.public_code}


def test_own_confirmation_removes_the_match(
    pending: Match, players: list[Player]
) -> None:
    confirm_match(pending, actor=players[2].account)
    assert _codes(players[2]) == set()


def test_teammate_confirmation_removes_the_match(
    pending: Match, players: list[Player]
) -> None:
    confirm_match(pending, actor=players[2].account)
    assert _codes(players[3]) == set()


def test_other_team_confirmation_keeps_the_match(
    pending: Match, players: list[Player]
) -> None:
    confirm_match(pending, actor=players[2].account)
    assert _codes(players[0]) == {pending.public_code}
    assert _codes(players[1]) == {pending.public_code}


def test_confirmed_match_is_not_listed(confirmed: Match, players: list[Player]) -> None:
    for player in players:
        assert _codes(player) == set()


def test_disputed_match_is_not_listed(pending: Match, players: list[Player]) -> None:
    dispute_match(pending, actor=players[2].account)
    for player in players:
        assert _codes(player) == set()


def test_voided_match_is_not_listed(
    pending: Match, players: list[Player], moderator: User
) -> None:
    void_match(pending, actor=moderator)
    for player in players:
        assert _codes(player) == set()


def test_confirmed_status_without_confirmations_is_not_listed(
    pending: Match, players: list[Player]
) -> None:
    raw_update(pending, status=MatchStatus.CONFIRMED)
    assert _codes(players[0]) == set()


def test_other_players_pending_match_is_not_listed(
    pending: Match, players: list[Player]
) -> None:
    outsider = PlayerFactory.create()
    other = MatchFactory.create()
    assert _codes(outsider) == set()
    assert pending.public_code not in _codes(outsider)
    assert other.public_code not in _codes(players[0])


def test_each_match_is_listed_once(pending: Match, players: list[Player]) -> None:
    assert awaiting_confirmation(players[0]).count() == 1


def test_lists_only_the_players_own_pending_matches(players: list[Player]) -> None:
    mine = MatchFactory.create(participants=False)
    MatchParticipant.objects.create(match=mine, player=players[0], team=Team.B)
    MatchParticipant.objects.create(match=mine, player=players[1], team=Team.A)
    MatchFactory.create()
    assert _codes(players[0]) == {mine.public_code}

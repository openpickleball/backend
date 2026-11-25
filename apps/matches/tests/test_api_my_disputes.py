"""The signed-in player's disputed matches (#272).

``GET /api/v1/me/matches/disputes`` lists the disputed matches the
signed-in Account's Player played in, newest first. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import Match, MatchFormat
from apps.matches.services import record_match
from apps.matches.workflow import dispute_match, withdraw_dispute
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

LIST = "/api/v1/me/matches/disputes"


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _match(players: list[Player], played_on: date) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=played_on,
        submitted_by_user=players[0].account,
    ).match


def _codes(response: Any) -> list[str]:
    assert response.status_code == 200
    return [item["code"] for item in response.json()["items"]]


def test_lists_own_disputed_matches_newest_first() -> None:
    players = [PlayerFactory.create() for _ in range(4)]
    older = dispute_match(_match(players, date(2026, 5, 1)), actor=players[0].account)
    newer = dispute_match(_match(players, date(2026, 6, 1)), actor=players[2].account)
    _match(players, date(2026, 7, 1))  # pending, not disputed
    others = [PlayerFactory.create() for _ in range(4)]
    dispute_match(_match(others, date(2026, 6, 2)), actor=others[0].account)

    for player in players:
        assert _codes(signed_in(player.account).get(LIST)) == [
            newer.public_code,
            older.public_code,
        ]
    assert signed_in(players[3].account).get(LIST).json()["count"] == 2


def test_a_match_leaves_the_list_once_the_dispute_is_withdrawn() -> None:
    players = [PlayerFactory.create() for _ in range(4)]
    match = dispute_match(_match(players, date(2026, 5, 1)), actor=players[0].account)
    withdraw_dispute(match, actor=players[0].account)

    assert _codes(signed_in(players[2].account).get(LIST)) == []


def test_an_account_with_no_player_gets_an_empty_page() -> None:
    response = signed_in(UserFactory.create()).get(LIST)

    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


def test_signed_out_gets_401() -> None:
    response = Client().get(LIST)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"

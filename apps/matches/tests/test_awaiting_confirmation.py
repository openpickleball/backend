"""Matches awaiting the signed-in player's confirmation (issue #192).

A Match is listed for a Player while it is pending, the Player played in
it, and no player on the Player's team has confirmed it. Made-up data
only.
"""

from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
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


# The API operation.

AWAITING = "/api/v1/me/matches/awaiting-confirmation"


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cache.clear()


def _client(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _get(account: Any, query: str = "") -> Any:
    return _client(account).get(f"{AWAITING}{query}")


def test_api_needs_a_signed_in_account() -> None:
    response = Client().get(AWAITING)
    assert response.status_code == 401
    assert response.json() == {
        "code": "unauthorized",
        "message": "Authentication required.",
        "errors": [],
    }


def test_api_account_without_player_gets_an_empty_page() -> None:
    response = _get(UserFactory.create())
    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


def test_api_lists_the_pending_match(pending: Match, players: list[Player]) -> None:
    response = _get(players[3].account)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert [item["code"] for item in body["items"]] == [pending.public_code]
    assert body["items"][0]["status"] == MatchStatus.PENDING


def test_api_counts_a_doubles_match_once(pending: Match, players: list[Player]) -> None:
    confirm_match(pending, actor=players[2].account)
    body = _get(players[1].account).json()
    assert body["count"] == 1
    assert len(body["items"]) == 1
    assert _get(players[3].account).json() == {"items": [], "count": 0}


def _matches_for(player: Player, count: int) -> list[Match]:
    made = []
    for _ in range(count):
        match = MatchFactory.create(participants=False)
        MatchParticipant.objects.create(match=match, player=player, team=Team.A)
        MatchParticipant.objects.create(
            match=match, player=PlayerFactory.create(), team=Team.B
        )
        made.append(match)
    return made


def _newest_first(matches: list[Match]) -> list[str]:
    ordered = sorted(
        matches, key=lambda m: (m.played_on, m.created_at, m.pk), reverse=True
    )
    return [match.public_code for match in ordered]


def test_api_pages_newest_first() -> None:
    player = PlayerFactory.create()
    matches = _matches_for(player, 5)
    expected = _newest_first(matches)
    body = _get(player.account, "?limit=2&offset=1").json()
    assert body["count"] == 5
    assert [item["code"] for item in body["items"]] == expected[1:3]


def test_api_default_page_size_is_20() -> None:
    player = PlayerFactory.create()
    _matches_for(player, 21)
    body = _get(player.account).json()
    assert body["count"] == 21
    assert len(body["items"]) == 20


@pytest.mark.parametrize(
    "query", ["?limit=0", "?limit=101", f"?offset={2**63}", "?offset=-1"]
)
def test_api_refuses_paging_out_of_range(query: str) -> None:
    player = PlayerFactory.create()
    response = _get(player.account, query)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_api_query_count_does_not_grow_with_matches(
    django_assert_max_num_queries: Any,
) -> None:
    player = PlayerFactory.create()
    client = _client(player.account)
    _matches_for(player, 1)
    with django_assert_max_num_queries(50) as one:
        client.get(AWAITING)
    _matches_for(player, 5)
    with django_assert_max_num_queries(50) as six:
        response = client.get(AWAITING)
    assert response.json()["count"] == 6
    assert len(six.captured_queries) == len(one.captured_queries)


def test_api_operation_is_in_the_document() -> None:
    document = Client().get("/api/v1/openapi.json").json()
    operation = document["paths"][AWAITING]["get"]
    assert operation["operationId"] == "list_matches_awaiting_confirmation"
    assert operation["tags"] == ["matches"]

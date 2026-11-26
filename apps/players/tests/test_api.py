"""A player's match history at /api/v1/players/{code}/matches (issue #31).

The player's own Account sees every match they played, in every status.
Another signed-in Account sees only confirmed matches, and only while the
player's ``match_history_visibility`` lets them (``PlayerPrivacy.visible_to``).
Made-up data only.
"""

from datetime import date, timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import confirm_match, dispute_match
from apps.players.models import Player, PlayerPrivacy, Visibility
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate

pytestmark = pytest.mark.django_db

FIRST_DAY = date(2026, 3, 2)
# A hidden history gets the unknown-code 404 (ADR 0008).
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def history(client: Client, code: str, **params: Any) -> Any:
    return client.get(f"/api/v1/players/{code}/matches", params)


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def play(players: list[Player], day: int) -> Match:
    """A pending singles match, players 0 against 1, ``day`` days in."""
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=[players[0]],
        team_b=[players[1]],
        games=[(11, day % 10)],
        played_on=FIRST_DAY + timedelta(days=day),
        submitted_by_user=players[0].account,
    ).match


def confirm(match: Match, players: list[Player]) -> Match:
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[1].account)


@pytest.fixture
def every_status(players: list[Player]) -> dict[str, Match]:
    """One match of player 0 in each status, newest last."""
    pending = play(players, 1)
    confirmed = confirm(play(players, 2), players)
    disputed = play(players, 3)
    dispute_match(disputed, actor=players[1].account)
    voided = play(players, 4)
    # Nobody voids a single self-reported match since #273; the history
    # reads only the stored status.
    raw_update(voided, status=MatchStatus.VOIDED)
    return {
        "pending": pending,
        "confirmed": confirmed,
        "disputed": disputed,
        "voided": voided,
    }


def _codes(response: Any) -> list[str]:
    return [item["code"] for item in response.json()["items"]]


def test_player_sees_every_status_newest_first(
    players: list[Player], every_status: dict[str, Match]
) -> None:
    response = history(signed_in(players[0].account), players[0].public_code)

    assert response.status_code == 200
    content = response.json()
    assert content["count"] == 4
    assert _codes(response) == [
        every_status[status].public_code
        for status in ("voided", "disputed", "confirmed", "pending")
    ]
    assert [item["status"] for item in content["items"]] == [
        "voided",
        "disputed",
        "confirmed",
        "pending",
    ]


def test_same_day_matches_are_newest_submitted_first(players: list[Player]) -> None:
    earlier = play(players, 1)
    later = record_match(
        format=MatchFormat.SINGLES,
        team_a=[players[0]],
        team_b=[players[2]],
        games=[(11, 3)],
        played_on=earlier.played_on,
        submitted_by_user=players[0].account,
    ).match

    response = history(signed_in(players[0].account), players[0].public_code)

    assert _codes(response) == [later.public_code, earlier.public_code]


def test_other_account_sees_only_confirmed_matches(
    players: list[Player], every_status: dict[str, Match]
) -> None:
    viewer = PlayerFactory.create().account

    response = history(signed_in(viewer), players[0].public_code)

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert _codes(response) == [every_status["confirmed"].public_code]


def test_account_without_a_player_sees_confirmed_matches(
    players: list[Player], every_status: dict[str, Match]
) -> None:
    response = history(signed_in(UserFactory.create()), players[0].public_code)

    assert response.status_code == 200
    assert _codes(response) == [every_status["confirmed"].public_code]


def test_private_history_is_404_to_others(
    players: list[Player], every_status: dict[str, Match]
) -> None:
    PlayerPrivacy.objects.filter(player=players[0]).update(
        match_history_visibility=Visibility.PRIVATE
    )

    response = history(signed_in(players[1].account), players[0].public_code)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_private_history_is_still_shown_to_the_player(
    players: list[Player], every_status: dict[str, Match]
) -> None:
    PlayerPrivacy.objects.filter(player=players[0]).update(
        match_history_visibility=Visibility.PRIVATE
    )

    response = history(signed_in(players[0].account), players[0].public_code)

    assert response.status_code == 200
    assert response.json()["count"] == 4


def test_history_of_a_player_whose_account_is_inactive_is_404(
    players: list[Player], every_status: dict[str, Match]
) -> None:
    get_user_model().objects.filter(pk=players[0].account_id).update(is_active=False)

    response = history(signed_in(players[1].account), players[0].public_code)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_inactive_opponent_shows_as_private_player(
    players: list[Player], every_status: dict[str, Match]
) -> None:
    get_user_model().objects.filter(pk=players[1].account_id).update(is_active=False)

    response = history(signed_in(players[0].account), players[0].public_code)

    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["team_b"] == [{"code": None, "display_name": "Private player"}]
    assert players[1].display_name not in response.content.decode()
    assert players[1].public_code not in response.content.decode()


def test_signed_out_gets_401(players: list[Player]) -> None:
    response = history(Client(), players[0].public_code)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


@pytest.mark.parametrize("code", ["not-a-player-code", "P-0000-0000-00"])
def test_unknown_or_malformed_code_gets_404(players: list[Player], code: str) -> None:
    response = history(signed_in(players[0].account), code)

    assert response.status_code == 404
    assert response.json() == {
        "code": "not_found",
        "message": "Not found.",
        "errors": [],
    }


def test_player_with_no_matches_has_an_empty_history(players: list[Player]) -> None:
    response = history(signed_in(players[3].account), players[3].public_code)

    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


def test_limit_and_offset_page_through_matches(
    players: list[Player], every_status: dict[str, Match]
) -> None:
    response = history(
        signed_in(players[0].account), players[0].public_code, limit=2, offset=1
    )

    assert response.status_code == 200
    assert response.json()["count"] == 4
    assert _codes(response) == [
        every_status["disputed"].public_code,
        every_status["confirmed"].public_code,
    ]


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 0}, "query.limit"),
        ({"limit": 101}, "query.limit"),
        ({"offset": -1}, "query.offset"),
    ],
)
def test_out_of_range_paging_gets_422(
    players: list[Player], params: dict[str, int], field: str
) -> None:
    response = history(signed_in(players[0].account), players[0].public_code, **params)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert [error["field"] for error in response.json()["errors"]] == [field]


def test_query_count_does_not_grow_with_matches(players: list[Player]) -> None:
    client = signed_in(players[0].account)
    confirm(play(players, 0), players)
    with CaptureQueriesContext(connection) as one:
        history(client, players[0].public_code)
    for day in range(1, 5):
        confirm(play(players, day), players)

    with CaptureQueriesContext(connection) as five:
        response = history(client, players[0].public_code)

    assert response.json()["count"] == 5
    assert len(response.json()["items"]) == 5
    assert len(five.captured_queries) == len(one.captured_queries)


def test_history_is_documented() -> None:
    document = Client().get("/api/v1/openapi.json").json()
    operation = document["paths"]["/api/v1/players/{code}/matches"]["get"]

    assert operation["operationId"] == "list_player_matches"
    assert set(operation["responses"]) >= {"200", "401", "404", "422"}
    # The only 403 is two-factor setup (ADR 0043): a hidden history is 404.
    assert operation["responses"]["403"]["description"].startswith(
        "Code `mfa_required`"
    )
    params = {param["name"]: param["schema"] for param in operation["parameters"]}
    assert params["limit"]["default"] == 20
    assert (params["limit"]["minimum"], params["limit"]["maximum"]) == (1, 100)
    assert params["offset"]["minimum"] == 0


def _well_formed_unknown_code() -> str:
    taken = set(Player.objects.values_list("public_code", flat=True))
    while (code := generate(CodeType.PLAYER)) in taken:
        pass
    return code


@pytest.mark.parametrize("hidden_by", ["private", "inactive"])
@pytest.mark.parametrize("unknown", ["well_formed", "malformed"])
def test_hidden_history_matches_any_unknown_code_404(
    players: list[Player], every_status: dict[str, Match], hidden_by: str, unknown: str
) -> None:
    if hidden_by == "private":
        PlayerPrivacy.objects.filter(player=players[0]).update(
            match_history_visibility=Visibility.PRIVATE
        )
    else:
        get_user_model().objects.filter(pk=players[0].account_id).update(
            is_active=False
        )
    code = _well_formed_unknown_code() if unknown == "well_formed" else "not-a-code"
    client = signed_in(players[1].account)

    refused = history(client, players[0].public_code)
    missing = history(client, code)

    assert refused.status_code == missing.status_code == 404
    assert refused.content == missing.content
    assert refused["Content-Type"] == missing["Content-Type"]

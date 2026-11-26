"""One match by its OPEN Code, for the players who played in it (issue #194).

``GET /api/v1/matches/{code}`` returns the match to each of its
participants, in every status. Anyone else gets the unknown-code 404,
an organizer of the match's Event included. Made-up data only.
"""

from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchStatus
from apps.matches.schemas import match_out, with_match_details
from apps.matches.tests.factories import (
    DoublesMatchFactory,
    MatchFactory,
    raw_update,
)
from apps.matches.workflow import confirm_match, dispute_match
from apps.players.models import Player

pytestmark = pytest.mark.django_db

MATCHES = "/api/v1/matches/"
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}
# Session, Account, the Match by code, the Account's active check, the
# viewer's Player, the Match again, then its participants, games, and
# confirmations.
QUERIES = 9


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def expected(match: Match, viewer: User) -> dict[str, Any]:
    loaded = with_match_details(Match.objects.filter(pk=match.pk)).get()
    body = match_out(loaded, viewer).model_dump(mode="json")
    body.pop("created_at")
    return body


def shown(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    body.pop("created_at")
    return body


@pytest.mark.parametrize("index", range(4))
def test_each_participant_gets_the_match(
    pending: Match, players: list[Player], index: int
) -> None:
    account = players[index].account

    response = signed_in(account).get(f"{MATCHES}{pending.public_code}")

    assert response.status_code == 200
    assert shown(response) == expected(pending, account)
    assert response.json()["my_team"] == ("A" if index < 2 else "B")


@pytest.mark.parametrize("status", ["pending", "confirmed", "disputed", "voided"])
def test_participant_gets_the_match_in_every_status(
    request: pytest.FixtureRequest,
    players: list[Player],
    status: str,
) -> None:
    fixture = "confirmed" if status == "confirmed" else "pending"
    match: Match = request.getfixturevalue(fixture)
    if status == "disputed":
        dispute_match(match, actor=players[2].account)
    elif status == "voided":
        raw_update(match, status=MatchStatus.VOIDED)

    response = signed_in(players[3].account).get(f"{MATCHES}{match.public_code}")

    assert response.status_code == 200
    assert response.json()["status"] == status
    assert shown(response) == expected(match, players[3].account)


def test_lowercase_code_finds_the_match(pending: Match, players: list[Player]) -> None:
    response = signed_in(players[0].account).get(
        f"{MATCHES}{pending.public_code.lower()}"
    )

    assert response.status_code == 200
    assert response.json()["code"] == pending.public_code


@pytest.mark.parametrize("code", ["M-0000-0000-0000", "not-a-code"])
def test_unknown_or_malformed_code_is_404(players: list[Player], code: str) -> None:
    response = signed_in(players[0].account).get(f"{MATCHES}{code}")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_signed_out_is_401(pending: Match) -> None:
    response = Client().get(f"{MATCHES}{pending.public_code}")

    assert response.status_code == 401
    assert response.json() == {
        "code": "unauthorized",
        "message": "Authentication required.",
        "errors": [],
    }


def test_event_organizer_who_did_not_play_gets_404(event: Event) -> None:
    match: Match = MatchFactory.create(event=event)
    member = OrganizerMemberFactory.create(organizer=event.organizer)

    response = signed_in(member.account).get(f"{MATCHES}{match.public_code}")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def _count(match: Match) -> tuple[int, int]:
    account = match.participants.order_by("pk").first().player.account  # type: ignore[union-attr]
    client = signed_in(account)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(f"{MATCHES}{match.public_code}")
    return response.status_code, len(queries)


def test_queries_do_not_grow_with_participants_or_games() -> None:
    singles = MatchFactory.create()
    doubles = DoublesMatchFactory.create()
    long_doubles = DoublesMatchFactory.create(best_of_5=True)
    assert singles.games.count() == doubles.games.count() == 1
    assert long_doubles.games.count() >= 3

    assert _count(singles) == _count(doubles) == _count(long_doubles)
    assert _count(long_doubles) == (200, QUERIES)


def test_participant_sees_their_own_decision(
    pending: Match, players: list[Player]
) -> None:
    confirm_match(pending, actor=players[0].account)
    dispute_match(pending, actor=players[2].account)

    def viewed(index: int) -> dict[str, Any]:
        response = signed_in(players[index].account).get(
            f"{MATCHES}{pending.public_code}"
        )
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return {
            key: body[key]
            for key in ("status", "my_team", "my_decision", "confirmed_by_team")
        }

    assert viewed(0) == {
        "status": "disputed",
        "my_team": "A",
        "my_decision": "confirm",
        "confirmed_by_team": {"a": True, "b": False},
    }
    assert viewed(1) == {
        "status": "disputed",
        "my_team": "A",
        "my_decision": "none",
        "confirmed_by_team": {"a": True, "b": False},
    }
    assert viewed(2) == {
        "status": "disputed",
        "my_team": "B",
        "my_decision": "dispute",
        "confirmed_by_team": {"a": True, "b": False},
    }

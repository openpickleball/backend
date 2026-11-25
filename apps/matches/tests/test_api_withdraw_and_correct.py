"""Players withdraw a dispute and correct a self-reported match (#267).

``withdraw_match_dispute`` withdraws the signed-in player's open dispute.
``correct_match`` lets a participant of a pending or disputed
self-reported match correct it with the same teams. Anyone else, and a
participant of a match with an Event, gets the same 404 as an unknown
code. Made-up data only.
"""

from typing import Any

import pytest
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import (
    ConfirmationDecision,
    Match,
    MatchConfirmation,
    MatchFormat,
    MatchStatus,
)
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON
from apps.matches.workflow import dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate
from config.api import api

pytestmark = pytest.mark.django_db

UNKNOWN_MATCH = "M-0000-0000-0000"
MALFORMED = "not-a-code"


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _same_404(refused: Any, unknown: Any) -> None:
    assert refused.status_code == unknown.status_code == 404
    assert refused.content == unknown.content
    assert refused["Content-Type"] == unknown["Content-Type"]


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def _withdraw(client: Client, code: str) -> Any:
    return client.post(f"/api/v1/matches/{code}/withdraw-dispute")


def _body(players: list[Player], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": "doubles",
        "played_on": WORKFLOW_PLAYED_ON.isoformat(),
        "team_a": [p.public_code for p in players[:2]],
        "team_b": [p.public_code for p in players[2:4]],
        "games": [{"team_a": 11, "team_b": 3}, {"team_a": 11, "team_b": 5}],
    }
    body.update(overrides)
    return body


def _correct(client: Client, code: str, body: dict[str, Any]) -> Any:
    return client.post(
        f"/api/v1/matches/{code}/correction", body, content_type="application/json"
    )


# Withdrawing a dispute.


def test_a_player_withdraws_their_dispute(
    confirmed: Match, players: list[Player]
) -> None:
    client = signed_in(players[0].account)
    disputed = client.post(f"/api/v1/matches/{confirmed.public_code}/dispute")
    assert disputed.json()["status"] == "disputed"

    response = _withdraw(client, confirmed.public_code)

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert response.json()["my_decision"] == "withdraw"
    read = client.get(f"/api/v1/matches/{confirmed.public_code}")
    assert read.status_code == 200
    assert read.json()["my_decision"] == "withdraw"


def test_disputing_again_after_withdrawing_changes_nothing(
    confirmed: Match, players: list[Player]
) -> None:
    client = signed_in(players[0].account)
    client.post(f"/api/v1/matches/{confirmed.public_code}/dispute")
    _withdraw(client, confirmed.public_code)

    response = client.post(f"/api/v1/matches/{confirmed.public_code}/dispute")

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"


def test_withdrawing_without_an_open_dispute_is_a_409(
    confirmed: Match, players: list[Player]
) -> None:
    response = _withdraw(signed_in(players[0].account), confirmed.public_code)

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert _status(confirmed) == MatchStatus.CONFIRMED


@pytest.mark.parametrize("missing", [UNKNOWN_MATCH, MALFORMED])
def test_a_non_participant_withdraw_gets_the_unknown_code_404(
    confirmed: Match, players: list[Player], missing: str
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    client = signed_in(PlayerFactory.create().account)

    _same_404(_withdraw(client, confirmed.public_code), _withdraw(client, missing))
    assert _status(confirmed) == MatchStatus.DISPUTED


def test_withdraw_needs_sign_in(confirmed: Match) -> None:
    response = Client().post(
        f"/api/v1/matches/{confirmed.public_code}/withdraw-dispute"
    )

    assert response.status_code == 401


# Correcting a self-reported match.


def test_a_player_corrects_a_disputed_match(
    confirmed: Match, players: list[Player]
) -> None:
    dispute_match(confirmed, actor=players[2].account)
    client = signed_in(players[2].account)

    response = _correct(client, confirmed.public_code, _body(players))

    assert response.status_code == 200
    data = response.json()
    assert data["code"] != confirmed.public_code
    assert data["status"] == "pending"
    assert data["my_decision"] == "confirm"
    assert data["confirmed_by_team"] == {"a": False, "b": True}
    assert data["games"] == [
        {"number": 1, "team_a": 11, "team_b": 3},
        {"number": 2, "team_a": 11, "team_b": 5},
    ]
    assert data["game_target"] == confirmed.game_target
    assert data["scoring_type"] == confirmed.scoring_type
    assert _status(confirmed) == MatchStatus.VOIDED
    corrected = Match.objects.get(public_code=data["code"])
    assert corrected.corrects_id == confirmed.pk
    assert list(
        MatchConfirmation.objects.filter(match=corrected).values_list(
            "player_id", "decision"
        )
    ) == [(players[2].pk, ConfirmationDecision.CONFIRM)]


def test_a_player_corrects_a_pending_match_with_swapped_teams(
    pending: Match, players: list[Player]
) -> None:
    body = _body(
        players,
        team_a=[p.public_code for p in players[2:]],
        team_b=[p.public_code for p in players[:2]],
    )

    response = _correct(signed_in(players[0].account), pending.public_code, body)

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert _status(pending) == MatchStatus.VOIDED


def test_correcting_a_confirmed_match_is_a_409(
    confirmed: Match, players: list[Player]
) -> None:
    response = _correct(
        signed_in(players[0].account), confirmed.public_code, _body(players)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert _status(confirmed) == MatchStatus.CONFIRMED


def test_an_unknown_player_code_is_a_422(pending: Match, players: list[Player]) -> None:
    body = _body(players, team_a=[generate(CodeType.PLAYER), players[1].public_code])

    response = _correct(signed_in(players[0].account), pending.public_code, body)

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "body.team_a.0"
    assert _status(pending) == MatchStatus.PENDING


def test_bad_scores_are_a_422_in_the_body(
    pending: Match, players: list[Player]
) -> None:
    body = _body(players, games=[{"team_a": 11, "team_b": 10}])

    response = _correct(signed_in(players[0].account), pending.public_code, body)

    assert response.status_code == 422
    assert all(
        error["field"].startswith("body.") for error in response.json()["errors"]
    )
    assert _status(pending) == MatchStatus.PENDING


def test_changed_teams_get_the_unknown_code_404(
    pending: Match, players: list[Player]
) -> None:
    client = signed_in(players[0].account)
    body = _body(
        players,
        team_a=[players[0].public_code, PlayerFactory.create().public_code],
    )

    _same_404(
        _correct(client, pending.public_code, body),
        _correct(client, UNKNOWN_MATCH, body),
    )
    assert _status(pending) == MatchStatus.PENDING


@pytest.mark.parametrize("missing", [UNKNOWN_MATCH, MALFORMED])
def test_a_non_participant_correction_gets_the_unknown_code_404(
    pending: Match, players: list[Player], missing: str
) -> None:
    for account in (PlayerFactory.create().account, UserFactory.create()):
        client = signed_in(account)
        for body in (
            _body(players),
            _body(players, team_a=[generate(CodeType.PLAYER), MALFORMED]),
        ):
            _same_404(
                _correct(client, pending.public_code, body),
                _correct(client, missing, body),
            )
    assert _status(pending) == MatchStatus.PENDING


def test_a_participant_of_an_event_match_gets_the_unknown_code_404(
    event: Event, players: list[Player]
) -> None:
    member = OrganizerMemberFactory.create(organizer=event.organizer).account
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=member,
        event=event,
    ).match
    dispute_match(match, actor=players[0].account)
    client = signed_in(players[0].account)

    _same_404(
        _correct(client, match.public_code, _body(players)),
        _correct(client, UNKNOWN_MATCH, _body(players)),
    )
    assert _status(match) == MatchStatus.DISPUTED


def test_correction_needs_sign_in(pending: Match, players: list[Player]) -> None:
    response = Client().post(
        f"/api/v1/matches/{pending.public_code}/correction",
        _body(players),
        content_type="application/json",
    )

    assert response.status_code == 401


def test_the_operations_are_documented() -> None:
    schema = api.get_openapi_schema()
    ids = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert {"withdraw_match_dispute", "correct_match"} <= ids

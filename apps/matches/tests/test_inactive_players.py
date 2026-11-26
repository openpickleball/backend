"""No new Match names a Player whose Account is inactive (#116, ADR 0048).

A deleted or deactivated Account's Player stays in the Ledger, but every
submission path treats it as unknown. A correction may still keep the
players of the Match it corrects. Made-up data only.
"""

from typing import Any

import pytest
from django.test import Client

from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import MatchValidationError, record_match
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

MATCHES = "/api/v1/matches/"


def _deactivate(player: Player) -> None:
    account = player.account
    account.is_active = False
    account.save(update_fields=["is_active"])


def _signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _body(players: list[Player]) -> dict[str, Any]:
    return {
        "format": "doubles",
        "played_on": WORKFLOW_PLAYED_ON.isoformat(),
        "team_a": [p.public_code for p in players[:2]],
        "team_b": [p.public_code for p in players[2:4]],
        "games": [{"team_a": 11, "team_b": 3}, {"team_a": 11, "team_b": 5}],
    }


def test_the_api_treats_an_inactive_players_code_as_unknown(
    players: list[Player],
) -> None:
    _deactivate(players[2])

    response = _signed_in(players[0].account).post(
        MATCHES, _body(players), content_type="application/json"
    )

    assert response.status_code == 422
    errors = response.json()["errors"]
    assert [(e["field"], e["code"]) for e in errors] == [
        ("body.team_b.0", "unknown_player")
    ]
    assert not Match.objects.exists()


def test_record_match_refuses_an_inactive_player() -> None:
    players = [PlayerFactory.create() for _ in range(4)]
    _deactivate(players[3])

    with pytest.raises(MatchValidationError) as raised:
        record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=WORKFLOW_GAMES,
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=players[0].account,
        )

    assert [(i.field, i.code) for i in raised.value.issues] == [
        ("team_b", "unknown_player")
    ]
    assert not Match.objects.exists()


def test_a_correction_keeps_an_inactive_player_of_the_corrected_match(
    pending: Match, players: list[Player]
) -> None:
    _deactivate(players[3])

    response = _signed_in(players[0].account).post(
        f"{MATCHES}{pending.public_code}/correction",
        _body(players),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.VOIDED


def test_a_correction_cannot_add_another_inactive_player(
    pending: Match, players: list[Player]
) -> None:
    outsider = PlayerFactory.create()
    _deactivate(outsider)
    body = _body([*players[:3], outsider])

    response = _signed_in(players[0].account).post(
        f"{MATCHES}{pending.public_code}/correction",
        body,
        content_type="application/json",
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "body.team_b.1"
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING

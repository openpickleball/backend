"""A player's submission confirms the match for their own team (issue #197).

``create_match`` records the submitter's confirmation in the same
transaction as the match. The match still needs a confirmation from the
other team: one team alone never confirms a match (hard rule 8). Made-up
data only.
"""

from typing import Any

import pytest
from django.test import Client

from apps.audit.models import AuditAction, AuditEvent
from apps.matches import workflow
from apps.matches.models import (
    ConfirmationDecision,
    Match,
    MatchConfirmation,
    MatchStatus,
)
from apps.matches.tests.conftest import SignalLog
from apps.matches.workflow import MatchStateError, awaiting_confirmation
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

MATCHES = "/api/v1/matches/"
GAMES = [{"team_a": 11, "team_b": 7}, {"team_a": 11, "team_b": 5}]


def signed_in(player: Player) -> Client:
    client = Client()
    client.force_login(player.account)
    return client


def submit(submitter: Player, team_a: list[Player], team_b: list[Player]) -> Any:
    body = {
        "format": "doubles" if len(team_a) == 2 else "singles",
        "played_on": "2026-06-01",
        "team_a": [p.public_code for p in team_a],
        "team_b": [p.public_code for p in team_b],
        "games": GAMES,
    }
    return signed_in(submitter).post(MATCHES, body, content_type="application/json")


def confirm(player: Player, code: str) -> Any:
    return signed_in(player).post(f"{MATCHES}{code}/confirm")


def confirm_events(match: Match) -> int:
    return AuditEvent.objects.filter(
        action=AuditAction.MATCH_CONFIRM, target_object_id=str(match.pk)
    ).count()


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def test_submission_records_the_submitters_confirmation(
    players: list[Player], signals: SignalLog
) -> None:
    response = submit(players[1], players[:2], players[2:])

    assert response.status_code == 201
    match = Match.objects.get()
    assert match.status == MatchStatus.PENDING
    assert list(
        MatchConfirmation.objects.filter(match=match).values_list(
            "player_id", "decision"
        )
    ) == [(players[1].pk, ConfirmationDecision.CONFIRM)]
    content = response.json()
    assert content["status"] == "pending"
    assert content["my_decision"] == "confirm"
    assert content["confirmed_by_team"] == {"a": True, "b": False}
    # Still pending: no status change, so no match.confirm and no signal.
    assert confirm_events(match) == 0
    assert signals.confirmed == []


def test_team_b_submitter_confirms_team_b(players: list[Player]) -> None:
    response = submit(players[3], players[:2], players[2:])

    assert response.status_code == 201
    assert response.json()["confirmed_by_team"] == {"a": False, "b": True}


def test_teammate_confirmation_never_confirms_the_match(
    players: list[Player], signals: SignalLog
) -> None:
    code = submit(players[0], players[:2], players[2:]).json()["code"]

    response = confirm(players[1], code)

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    match = Match.objects.get()
    assert match.status == MatchStatus.PENDING
    assert confirm_events(match) == 0
    assert signals.confirmed == []


def test_singles_needs_the_opponent(players: list[Player], signals: SignalLog) -> None:
    created = submit(players[0], players[:1], players[1:2])
    assert created.status_code == 201
    assert created.json()["status"] == "pending"
    code = created.json()["code"]

    # The submitter's own repeat confirmation changes nothing.
    again = confirm(players[0], code)
    assert again.json()["status"] == "pending"
    match = Match.objects.get()
    assert MatchConfirmation.objects.filter(match=match).count() == 1
    assert signals.confirmed == []

    response = confirm(players[1], code)

    assert response.json()["status"] == "confirmed"
    match.refresh_from_db()
    assert match.status == MatchStatus.CONFIRMED
    assert confirm_events(match) == 1
    assert signals.confirmed == [match.pk]


def test_other_team_confirmation_confirms_a_doubles_match(
    players: list[Player], signals: SignalLog
) -> None:
    code = submit(players[0], players[:2], players[2:]).json()["code"]

    response = confirm(players[3], code)

    assert response.json()["status"] == "confirmed"
    assert response.json()["confirmed_by_team"] == {"a": True, "b": True}
    match = Match.objects.get()
    assert confirm_events(match) == 1
    event = AuditEvent.objects.get(action=AuditAction.MATCH_CONFIRM)
    assert event.actor_user_id == players[3].account_id
    assert signals.confirmed == [match.pk]


def test_submitter_is_not_asked_to_confirm(players: list[Player]) -> None:
    code = submit(players[0], players[:2], players[2:]).json()["code"]

    assert not awaiting_confirmation(players[0]).exists()
    assert not awaiting_confirmation(players[1]).exists()
    for opponent in players[2:]:
        assert [m.public_code for m in awaiting_confirmation(opponent)] == [code]


def test_awaiting_list_endpoint_leaves_out_the_submitters_match(
    players: list[Player],
) -> None:
    submit(players[0], players[:2], players[2:])

    own = signed_in(players[0]).get("/api/v1/me/matches/awaiting-confirmation")
    other = signed_in(players[2]).get("/api/v1/me/matches/awaiting-confirmation")

    assert own.json()["count"] == 0
    assert other.json()["count"] == 1


def test_failed_confirmation_rolls_back_the_match(
    players: list[Player], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(match: Match, *, actor: object) -> Match:
        raise MatchStateError

    monkeypatch.setattr(workflow, "confirm_match", refuse)

    response = submit(players[0], players[:2], players[2:])

    assert response.status_code == 409
    assert not Match.objects.exists()
    assert not MatchConfirmation.objects.exists()


def test_exact_duplicate_from_the_other_team_is_still_409(
    players: list[Player],
) -> None:
    first = submit(players[0], players[:2], players[2:])

    response = submit(players[2], players[:2], players[2:])

    assert response.status_code == 409
    assert response.json()["code"] == "duplicate_match"
    match = Match.objects.get()
    assert match.public_code == first.json()["code"]
    assert match.status == MatchStatus.PENDING
    assert list(
        MatchConfirmation.objects.filter(match=match).values_list(
            "player_id", flat=True
        )
    ) == [players[0].pk]

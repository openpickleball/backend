"""The first-party match endpoints at /api/v1/matches/ (issue #31).

A signed-in player submits a match they played, and the participants
confirm or dispute it. Every response names players by OPEN Code, and
every refusal is fixed text that never repeats a submitted value. Made-up
data only.
"""

from datetime import datetime
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    app_headers,
    login,
    signup,
    verify_from_last_mail,
)
from apps.matches.models import Game, Match, MatchParticipant, MatchStatus, Team
from apps.matches.workflow import void_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate

# Queued account email is sent when each request returns (ADR 0016).
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

MATCHES = "/api/v1/matches/"
PLAYED_ON = "2026-06-01"
GAMES = [
    {"team_a": 11, "team_b": 7},
    {"team_a": 9, "team_b": 11},
    {"team_a": 11, "team_b": 4},
]
UNAUTHORIZED = {
    "code": "unauthorized",
    "message": "Authentication required.",
    "errors": [],
}
SUBMIT_FORBIDDEN = {
    "code": "forbidden",
    "message": "You are not allowed to submit this match.",
    "errors": [],
}
UNKNOWN_PLAYER = "No player has this OPEN Code."


def signed_in(account: Any) -> Client:
    """A client signed in as ``account`` with a session cookie."""
    client = Client()
    client.force_login(account)
    return client


def payload(players: list[Player], **overrides: Any) -> dict[str, Any]:
    """A doubles submission: players 0 and 1 against 2 and 3."""
    body: dict[str, Any] = {
        "format": "doubles",
        "played_on": PLAYED_ON,
        "team_a": [p.public_code for p in players[:2]],
        "team_b": [p.public_code for p in players[2:4]],
        "games": GAMES,
    }
    body.update(overrides)
    return body


def submit(client: Client, body: dict[str, Any]) -> Any:
    return client.post(MATCHES, body, content_type="application/json")


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def _team(players: list[Player]) -> list[dict[str, Any]]:
    return [{"code": p.public_code, "display_name": p.display_name} for p in players]


# Submitting a match.


def test_participant_submits_a_pending_match_with_defaults(
    players: list[Player],
) -> None:
    response = submit(signed_in(players[0].account), payload(players))

    assert response.status_code == 201
    match = Match.objects.get()
    content = response.json()
    # Django's JSON encoder keeps milliseconds, and none on an exact second,
    # so compare the parsed time.
    shown = datetime.fromisoformat(content.pop("created_at"))
    assert shown == match.created_at.replace(
        microsecond=match.created_at.microsecond // 1000 * 1000
    )
    assert content == {
        "code": match.public_code,
        "status": "pending",
        "format": "doubles",
        "played_on": PLAYED_ON,
        "best_of": 3,
        "scoring_type": "side_out",
        "game_target": 11,
        "team_a": _team(players[:2]),
        "team_b": _team(players[2:]),
        "games": [{"number": n, **game} for n, game in enumerate(GAMES, start=1)],
        "my_team": "A",
        "my_decision": "none",
        "confirmed_by_team": {"a": False, "b": False},
    }
    assert match.status == MatchStatus.PENDING
    assert match.submitted_by_user_id == players[0].account_id
    assert match.submitted_by_application_id is None
    assert match.event_id is None
    assert match.club_id is None
    assert {
        (p.player_id, p.team) for p in MatchParticipant.objects.filter(match=match)
    } == {(p.pk, Team.A) for p in players[:2]} | {(p.pk, Team.B) for p in players[2:]}
    assert [
        (g.game_number, g.team_a_score, g.team_b_score)
        for g in Game.objects.filter(match=match)
    ] == [(1, 11, 7), (2, 9, 11), (3, 11, 4)]


def test_submission_keeps_the_chosen_settings(players: list[Player]) -> None:
    body = payload(
        players,
        format="singles",
        team_a=[players[0].public_code],
        team_b=[players[2].public_code],
        games=[{"team_a": 15, "team_b": 13}],
        best_of=1,
        scoring_type="rally",
        game_target=15,
    )

    response = submit(signed_in(players[2].account), body)

    assert response.status_code == 201
    content = response.json()
    assert (content["best_of"], content["scoring_type"], content["game_target"]) == (
        1,
        "rally",
        15,
    )


def test_codes_are_read_in_any_case_without_hyphens(players: list[Player]) -> None:
    team_a = [p.public_code.lower().replace("-", "") for p in players[:2]]

    response = submit(signed_in(players[0].account), payload(players, team_a=team_a))

    assert response.status_code == 201
    assert [p["code"] for p in response.json()["team_a"]] == [
        p.public_code for p in players[:2]
    ]


def test_app_client_session_token_submits(players: list[Player]) -> None:
    cache.clear()
    email = "token.submitter@example.org"
    signup(Client(), APP, email)
    verify_from_last_mail(Client(), APP)
    token = login(Client(), APP, email).json()["meta"]["session_token"]
    account = get_user_model().objects.get_by_natural_key(email)
    submitter = PlayerFactory.create(account=account)
    lineup = [submitter, *players[1:]]

    response = Client().post(
        MATCHES,
        payload(lineup),
        content_type="application/json",
        headers=app_headers(token),
    )
    cache.clear()

    assert response.status_code == 201
    assert Match.objects.get().submitted_by_user_id == account.pk


def test_signed_out_submission_gets_401(players: list[Player]) -> None:
    response = submit(Client(), payload(players))

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED
    assert not Match.objects.exists()


def test_inactive_account_gets_401(players: list[Player]) -> None:
    client = signed_in(players[0].account)
    players[0].account.is_active = False
    players[0].account.save(update_fields=["is_active"])

    response = submit(client, payload(players))

    assert response.status_code == 401
    assert not Match.objects.exists()


def test_non_participant_gets_403_with_fixed_text(players: list[Player]) -> None:
    outsider = PlayerFactory.create()

    response = submit(signed_in(outsider.account), payload(players))

    assert response.status_code == 403
    assert response.json() == SUBMIT_FORBIDDEN
    assert not Match.objects.exists()


def test_account_without_a_player_gets_403(players: list[Player]) -> None:
    response = submit(signed_in(UserFactory.create()), payload(players))

    assert response.status_code == 403
    assert response.json() == SUBMIT_FORBIDDEN
    assert not Match.objects.exists()


def test_non_participant_learns_nothing_about_codes_or_scores(
    players: list[Player],
) -> None:
    # The permission check comes before code lookup and validation, so a
    # refused submitter cannot probe which codes exist or which scores fail.
    outsider = PlayerFactory.create()
    body = payload(
        players,
        team_b=["P-0000-0000-00", "not-a-code"],
        games=[{"team_a": 11, "team_b": 11}],
    )

    response = submit(signed_in(outsider.account), body)

    assert response.status_code == 403
    assert response.json() == SUBMIT_FORBIDDEN


def test_non_participant_gets_403_for_an_existing_match(players: list[Player]) -> None:
    assert submit(signed_in(players[0].account), payload(players)).status_code == 201
    outsider = PlayerFactory.create()

    response = submit(signed_in(outsider.account), payload(players))

    assert response.status_code == 403
    assert response.json() == SUBMIT_FORBIDDEN
    assert Match.objects.count() == 1


def _unknown_code(players: list[Player]) -> str:
    """A well-formed Player code that no Player has."""
    taken = {p.public_code for p in Player.objects.all()}
    while (code := generate(CodeType.PLAYER)) in taken:
        pass
    return code


def test_malformed_and_unknown_codes_get_the_same_422_item(
    players: list[Player],
) -> None:
    unknown = _unknown_code(players)
    malformed = "zz-made-up-code"
    body = payload(
        players,
        team_a=[players[0].public_code, malformed],
        team_b=[unknown, players[3].public_code],
    )

    response = submit(signed_in(players[0].account), body)

    assert response.status_code == 422
    assert response.json() == {
        "code": "validation_error",
        "message": "The request is not valid.",
        "errors": [
            {
                "field": "body.team_a.1",
                "code": "unknown_player",
                "message": UNKNOWN_PLAYER,
            },
            {
                "field": "body.team_b.0",
                "code": "unknown_player",
                "message": UNKNOWN_PLAYER,
            },
        ],
    }
    text = response.content.decode()
    assert malformed not in text
    assert unknown not in text
    assert not Match.objects.exists()


def test_score_issues_point_at_request_fields(players: list[Player]) -> None:
    body = payload(
        players,
        team_b=[players[2].public_code],
        games=[{"team_a": 11, "team_b": 11}, {"team_a": 11, "team_b": 4}],
    )

    response = submit(signed_in(players[0].account), body)

    assert response.status_code == 422
    content = response.json()
    assert content["code"] == "validation_error"
    fields = {(e["field"], e["code"]) for e in content["errors"]}
    assert ("body.team_b", "team_size") in fields
    assert ("body.games.0", "tie") in fields
    assert not Match.objects.exists()


def test_future_date_points_at_played_on(players: list[Player]) -> None:
    response = submit(
        signed_in(players[0].account), payload(players, played_on="2999-01-01")
    )

    assert response.status_code == 422
    assert {(e["field"], e["code"]) for e in response.json()["errors"]} == {
        ("body.played_on", "future_date")
    }


def test_invalid_choice_is_a_422(players: list[Player]) -> None:
    response = submit(signed_in(players[0].account), payload(players, best_of=2))

    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["body.best_of"]
    assert not Match.objects.exists()


def test_exact_duplicate_gets_409(players: list[Player]) -> None:
    first = submit(signed_in(players[0].account), payload(players))
    assert first.status_code == 201

    response = submit(signed_in(players[2].account), payload(players))

    assert response.status_code == 409
    content = response.json()
    assert content["code"] == "duplicate_match"
    # The submitter played in the earlier match, so it is named.
    assert content["message"] == (
        f"This match was already submitted as {first.json()['code']}."
    )
    assert Match.objects.count() == 1


def test_create_operation_documents_only_first_party_fields() -> None:
    document = Client().get("/api/v1/openapi.json").json()
    operation = document["paths"][MATCHES]["post"]

    assert operation["operationId"] == "create_match"
    ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema = document["components"]["schemas"][ref.rsplit("/", 1)[1]]
    assert set(schema["properties"]) == {
        "format",
        "played_on",
        "team_a",
        "team_b",
        "games",
        "best_of",
        "scoring_type",
        "game_target",
    }
    assert set(operation["responses"]) >= {"201", "401", "403", "409", "422"}


# Confirming and disputing a match.

# A refused actor gets the unknown-code 404 (ADR 0008).
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}
CONFLICT = {
    "code": "conflict",
    "message": "This match cannot make this change in its current status.",
    "errors": [],
}


def act(client: Client, match_code: str, action: str) -> Any:
    return client.post(f"{MATCHES}{match_code}/{action}")


def test_both_teams_confirm_a_pending_match(
    pending: Match, players: list[Player]
) -> None:
    first = act(signed_in(players[0].account), pending.public_code, "confirm")

    assert first.status_code == 200
    assert first.json()["status"] == "pending"
    assert first.json()["code"] == pending.public_code

    second = act(signed_in(players[3].account), pending.public_code, "confirm")

    assert second.status_code == 200
    assert second.json()["status"] == "confirmed"
    assert second.json()["team_a"] == _team(players[:2])
    pending.refresh_from_db()
    assert pending.status == MatchStatus.CONFIRMED


def test_repeat_confirmation_changes_nothing(
    confirmed: Match, players: list[Player]
) -> None:
    response = act(signed_in(players[0].account), confirmed.public_code, "confirm")

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"


def test_code_is_read_in_any_case(pending: Match, players: list[Player]) -> None:
    code = pending.public_code.lower()

    response = act(signed_in(players[0].account), code, "confirm")

    assert response.status_code == 200
    assert response.json()["code"] == pending.public_code


@pytest.mark.parametrize("action", ["confirm", "dispute"])
def test_non_participant_gets_404(
    pending: Match, players: list[Player], action: str
) -> None:
    outsider = PlayerFactory.create()

    response = act(signed_in(outsider.account), pending.public_code, action)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    pending.refresh_from_db()
    assert pending.status == MatchStatus.PENDING


@pytest.mark.parametrize("action", ["confirm", "dispute"])
def test_account_without_player_gets_404(pending: Match, action: str) -> None:
    response = act(signed_in(UserFactory.create()), pending.public_code, action)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize("action", ["confirm", "dispute"])
def test_signed_out_gets_401(pending: Match, action: str) -> None:
    response = act(Client(), pending.public_code, action)

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


@pytest.mark.parametrize("action", ["confirm", "dispute"])
@pytest.mark.parametrize("code", ["not-a-match-code", "M-0000-0000-0000"])
def test_unknown_or_malformed_code_gets_404(
    players: list[Player], action: str, code: str
) -> None:
    response = act(signed_in(players[0].account), code, action)

    assert response.status_code == 404
    assert response.json() == {
        "code": "not_found",
        "message": "Not found.",
        "errors": [],
    }


def test_participant_disputes_a_match(confirmed: Match, players: list[Player]) -> None:
    response = act(signed_in(players[2].account), confirmed.public_code, "dispute")

    assert response.status_code == 200
    assert response.json()["status"] == "disputed"
    confirmed.refresh_from_db()
    assert confirmed.status == MatchStatus.DISPUTED


def test_repeat_dispute_changes_nothing(pending: Match, players: list[Player]) -> None:
    client = signed_in(players[2].account)
    assert act(client, pending.public_code, "dispute").status_code == 200

    response = act(client, pending.public_code, "dispute")

    assert response.status_code == 200
    assert response.json()["status"] == "disputed"


def test_confirming_a_disputed_match_gets_409(
    pending: Match, players: list[Player]
) -> None:
    act(signed_in(players[2].account), pending.public_code, "dispute")

    response = act(signed_in(players[0].account), pending.public_code, "confirm")

    assert response.status_code == 409
    assert response.json() == CONFLICT


@pytest.mark.parametrize("action", ["confirm", "dispute"])
def test_voided_match_gets_409(
    pending: Match, players: list[Player], moderator: Any, action: str
) -> None:
    void_match(pending, actor=moderator, reason="Entered by mistake.")

    response = act(signed_in(players[0].account), pending.public_code, action)

    assert response.status_code == 409
    assert response.json() == CONFLICT
    pending.refresh_from_db()
    assert pending.status == MatchStatus.VOIDED


def test_confirm_response_shows_inactive_participant_as_private(
    pending: Match, players: list[Player]
) -> None:
    get_user_model().objects.filter(pk=players[3].account_id).update(is_active=False)

    response = act(signed_in(players[0].account), pending.public_code, "confirm")

    assert response.status_code == 200
    assert response.json()["team_b"][1] == {
        "code": None,
        "display_name": "Private player",
    }
    assert players[3].display_name not in response.content.decode()


def test_confirm_and_dispute_are_documented() -> None:
    paths = Client().get("/api/v1/openapi.json").json()["paths"]

    for action in ("confirm", "dispute"):
        operation = paths[f"{MATCHES}{{code}}/{action}"]["post"]
        assert operation["operationId"] == f"{action}_match"
        assert set(operation["responses"]) >= {"200", "401", "403", "404", "409"}

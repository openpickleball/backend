"""The viewer's own fields on every match response (issue #193).

Each operation that returns a match adds ``my_team``, ``my_decision``,
``confirmed_by_team``, and ``created_at``, as the signed-in Account sees
the match. A viewer who did not play in it gets nulls and ``none``.
Nothing names who confirmed or disputed. Made-up data only.
"""

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from apps.accounts.models import User
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchStatus
from apps.matches.tests.factories import MatchFactory
from apps.matches.workflow import confirm_match, dispute_match, resolve_dispute
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

MATCHES = "/api/v1/matches/"
AWAITING = "/api/v1/me/matches/awaiting-confirmation"
VIEWER_KEYS = {"my_team", "my_decision", "confirmed_by_team", "created_at"}
MATCH_KEYS = {
    "code",
    "status",
    "format",
    "played_on",
    "best_of",
    "scoring_type",
    "game_target",
    "team_a",
    "team_b",
    "games",
} | VIEWER_KEYS


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cache.clear()


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def viewer_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in VIEWER_KEYS - {"created_at"}}


def assert_created_at(item: dict[str, Any], match: Match) -> None:
    """The response's ``created_at`` is the Match's, to the millisecond
    (Django's JSON encoder keeps milliseconds), and in UTC."""
    shown = datetime.fromisoformat(item["created_at"])
    assert shown.utcoffset() == timedelta(0)
    assert shown == match.created_at.replace(
        microsecond=match.created_at.microsecond // 1000 * 1000
    )


def test_create_response_shows_the_submitters_team(players: list[Player]) -> None:
    body = {
        "format": "doubles",
        "played_on": "2026-06-01",
        "team_a": [p.public_code for p in players[:2]],
        "team_b": [p.public_code for p in players[2:]],
        "games": [{"team_a": 11, "team_b": 7}, {"team_a": 11, "team_b": 5}],
    }

    response = signed_in(players[2].account).post(
        MATCHES, body, content_type="application/json"
    )

    assert response.status_code == 201
    item = response.json()
    assert set(item) == MATCH_KEYS
    assert viewer_fields(item) == {
        "my_team": "B",
        # The submission is the submitter's confirmation (#197).
        "my_decision": "confirm",
        "confirmed_by_team": {"a": False, "b": True},
    }
    assert_created_at(item, Match.objects.get())


def test_confirm_response_reflects_the_new_confirmation(
    pending: Match, players: list[Player]
) -> None:
    response = signed_in(players[2].account).post(
        f"{MATCHES}{pending.public_code}/confirm"
    )

    assert response.status_code == 200
    item = response.json()
    assert set(item) == MATCH_KEYS
    assert item["status"] == "pending"
    assert viewer_fields(item) == {
        "my_team": "B",
        "my_decision": "confirm",
        "confirmed_by_team": {"a": False, "b": True},
    }
    assert_created_at(item, pending)


def test_confirm_by_both_teams_shows_both_confirmed(
    pending: Match, players: list[Player]
) -> None:
    confirm_match(pending, actor=players[1].account)

    response = signed_in(players[3].account).post(
        f"{MATCHES}{pending.public_code}/confirm"
    )

    item = response.json()
    assert item["status"] == "confirmed"
    assert viewer_fields(item) == {
        "my_team": "B",
        "my_decision": "confirm",
        "confirmed_by_team": {"a": True, "b": True},
    }


def test_dispute_response_reflects_the_new_dispute(
    pending: Match, players: list[Player]
) -> None:
    confirm_match(pending, actor=players[0].account)

    response = signed_in(players[0].account).post(
        f"{MATCHES}{pending.public_code}/dispute"
    )

    assert response.status_code == 200
    item = response.json()
    assert set(item) == MATCH_KEYS
    assert item["status"] == "disputed"
    assert viewer_fields(item) == {
        "my_team": "A",
        "my_decision": "dispute",
        "confirmed_by_team": {"a": True, "b": False},
    }


def test_awaiting_list_shows_the_other_teams_confirmation(
    pending: Match, players: list[Player]
) -> None:
    confirm_match(pending, actor=players[0].account)

    response = signed_in(players[3].account).get(AWAITING)

    assert response.status_code == 200
    [item] = response.json()["items"]
    assert set(item) == MATCH_KEYS
    assert viewer_fields(item) == {
        "my_team": "B",
        "my_decision": "none",
        "confirmed_by_team": {"a": True, "b": False},
    }
    assert_created_at(item, pending)


def test_history_read_by_another_account_has_no_viewer_fields(
    confirmed: Match, players: list[Player]
) -> None:
    outsider = PlayerFactory.create()

    response = signed_in(outsider.account).get(
        f"/api/v1/players/{players[0].public_code}/matches"
    )

    assert response.status_code == 200
    [item] = response.json()["items"]
    assert set(item) == MATCH_KEYS
    assert viewer_fields(item) == {
        "my_team": None,
        "my_decision": "none",
        "confirmed_by_team": None,
    }
    assert_created_at(item, confirmed)
    for player in players:
        assert player.public_code not in str({k: item[k] for k in VIEWER_KEYS})


def test_history_read_by_the_player_shows_their_fields(
    confirmed: Match, players: list[Player]
) -> None:
    response = signed_in(players[0].account).get(
        f"/api/v1/players/{players[0].public_code}/matches"
    )

    [item] = response.json()["items"]
    assert viewer_fields(item) == {
        "my_team": "A",
        "my_decision": "confirm",
        "confirmed_by_team": {"a": True, "b": True},
    }


# The fields show recorded player decisions only.


def test_repeat_confirm_after_a_resolved_dispute_still_shows_dispute(
    pending: Match, players: list[Player], moderator: User
) -> None:
    confirm_match(pending, actor=players[0].account)
    dispute_match(pending, actor=players[0].account)
    resolve_dispute(
        pending,
        actor=moderator,
        outcome=MatchStatus.CONFIRMED,
        reason="Scores checked with both teams.",
    )

    response = signed_in(players[0].account).post(
        f"{MATCHES}{pending.public_code}/confirm"
    )

    # The confirmation was recorded already, so no new row is added, and
    # the latest recorded decision is still the dispute.
    assert response.status_code == 200
    item = response.json()
    assert item["status"] == "confirmed"
    assert viewer_fields(item) == {
        "my_team": "A",
        "my_decision": "dispute",
        "confirmed_by_team": {"a": True, "b": False},
    }


def test_organizer_member_who_plays_confirms_as_organizer(event: Event) -> None:
    match: Match = MatchFactory.create(event=event)
    participant = match.participants.get(team="A")
    account = participant.player.account
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)

    response = signed_in(account).post(f"{MATCHES}{match.public_code}/confirm")

    # The organizer path records no player decision, so the match is
    # confirmed with no team confirmation.
    assert response.status_code == 200
    item = response.json()
    assert item["status"] == "confirmed"
    assert viewer_fields(item) == {
        "my_team": "A",
        "my_decision": "none",
        "confirmed_by_team": {"a": False, "b": False},
    }


def test_new_fields_are_documented() -> None:
    schemas = Client().get("/api/v1/openapi.json").json()["components"]["schemas"]

    match = schemas["MatchOut"]
    assert set(match["required"]) >= VIEWER_KEYS
    description = " ".join(match["description"].split())
    assert "A repeated decision is not recorded again" in description
    assert "An organizer's confirmation is not a player decision" in description
    assert "read ``status`` for whether the match is confirmed" in description
    assert "whether the match counts" not in description
    assert (
        "A confirmation still shows in ``confirmed_by_team`` after the same "
        "player disputes" in description
    )
    assert "still counts" not in description
    assert set(schemas["TeamConfirmationsOut"]["properties"]) == {"a", "b"}

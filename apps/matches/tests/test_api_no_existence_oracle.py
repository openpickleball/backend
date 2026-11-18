"""A refused actor learns nothing about whether a record exists (ADR 0008).

A non-participant's read, confirm, or dispute of a real match, and
another Account's read of a private or inactive player's match history, get
exactly the 404 ``ErrorResponse`` that an unknown or malformed code gets.
Made-up data only.
"""

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchStatus
from apps.matches.tests.factories import MatchFactory
from apps.players.models import Player, PlayerPrivacy, Visibility
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

UNKNOWN_MATCH = "M-0000-0000-0000"
MALFORMED = "not-a-code"


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _same(refused: Any, unknown: Any) -> None:
    assert refused.status_code == unknown.status_code == 404
    assert refused.content == unknown.content
    assert refused["Content-Type"] == unknown["Content-Type"]


@pytest.mark.parametrize("action", ["confirm", "dispute"])
@pytest.mark.parametrize("missing", [UNKNOWN_MATCH, MALFORMED])
def test_non_participant_gets_the_unknown_code_404(
    pending: Match, action: str, missing: str
) -> None:
    client = signed_in(PlayerFactory.create().account)

    refused = client.post(f"/api/v1/matches/{pending.public_code}/{action}")
    unknown = client.post(f"/api/v1/matches/{missing}/{action}")

    _same(refused, unknown)
    pending.refresh_from_db()
    assert pending.status == MatchStatus.PENDING


@pytest.mark.parametrize("action", ["confirm", "dispute"])
def test_account_without_player_gets_the_unknown_code_404(
    confirmed: Match, action: str
) -> None:
    client = signed_in(UserFactory.create())

    refused = client.post(f"/api/v1/matches/{confirmed.public_code}/{action}")
    unknown = client.post(f"/api/v1/matches/{UNKNOWN_MATCH}/{action}")

    _same(refused, unknown)
    confirmed.refresh_from_db()
    assert confirmed.status == MatchStatus.CONFIRMED


@pytest.mark.parametrize("missing", [UNKNOWN_MATCH, MALFORMED])
def test_non_participant_read_gets_the_unknown_code_404(
    pending: Match, missing: str
) -> None:
    client = signed_in(PlayerFactory.create().account)

    refused = client.get(f"/api/v1/matches/{pending.public_code}")
    unknown = client.get(f"/api/v1/matches/{missing}")

    _same(refused, unknown)


def test_account_without_player_read_gets_the_unknown_code_404(
    confirmed: Match,
) -> None:
    client = signed_in(UserFactory.create())

    refused = client.get(f"/api/v1/matches/{confirmed.public_code}")
    unknown = client.get(f"/api/v1/matches/{UNKNOWN_MATCH}")

    _same(refused, unknown)


def test_event_organizer_read_gets_the_unknown_code_404(event: Event) -> None:
    match: Match = MatchFactory.create(event=event)
    member = OrganizerMemberFactory.create(organizer=event.organizer)
    client = signed_in(member.account)

    refused = client.get(f"/api/v1/matches/{match.public_code}")
    unknown = client.get(f"/api/v1/matches/{UNKNOWN_MATCH}")

    _same(refused, unknown)


def _unknown_history(client: Client) -> Any:
    return client.get(f"/api/v1/players/{MALFORMED}/matches")


def test_private_history_gets_the_unknown_code_404(
    confirmed: Match, players: list[Player]
) -> None:
    PlayerPrivacy.objects.filter(player=players[0]).update(
        match_history_visibility=Visibility.PRIVATE
    )
    client = signed_in(players[1].account)

    refused = client.get(f"/api/v1/players/{players[0].public_code}/matches")

    _same(refused, _unknown_history(client))


def test_inactive_players_history_gets_the_unknown_code_404(
    confirmed: Match, players: list[Player]
) -> None:
    get_user_model().objects.filter(pk=players[0].account_id).update(is_active=False)
    client = signed_in(players[1].account)

    refused = client.get(f"/api/v1/players/{players[0].public_code}/matches")

    _same(refused, _unknown_history(client))


def test_participant_state_error_is_still_409(
    pending: Match, players: list[Player]
) -> None:
    signed_in(players[2].account).post(f"/api/v1/matches/{pending.public_code}/dispute")

    response = signed_in(players[0].account).post(
        f"/api/v1/matches/{pending.public_code}/confirm"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"

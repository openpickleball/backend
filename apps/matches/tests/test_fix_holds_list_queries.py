"""The holds list's cost does not grow with the held matches (#270).

``GET /api/v1/me/matches/holds`` pages the held matches an Account may
vouch for. Deciding which ones it may vouch for must not cost queries per
held candidate beyond the page, or a busy Organizer's list slows with
every held match. Made-up data only.
"""

from datetime import date, timedelta

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import MatchFormat
from apps.matches.services import record_match
from apps.matches.workflow import confirm_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

HOLDS = "/api/v1/me/matches/holds?limit=1"
FIRST_DAY = date(2026, 5, 1)


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=account, role=MemberRole.MATCH_MANAGER
    )
    return account


def _self_reported_pair(players: list[Player], played_on: date) -> None:
    """Two confirmed self-reported matches a possible duplicate flag holds."""
    for games in (((11, 7), (11, 9)), ((11, 5), (11, 6))):
        match = record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=games,
            played_on=played_on,
            submitted_by_user=players[0].account,
        ).match
        confirm_match(match, actor=players[0].account)
        confirm_match(match, actor=players[2].account)


def _event_pair(event: Event, member: User, played_on: date) -> None:
    """Two Event matches a possible duplicate flag holds."""
    players = [PlayerFactory.create() for _ in range(4)]
    for games in (((11, 7), (11, 9)), ((11, 5), (11, 6))):
        record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=games,
            played_on=played_on,
            submitted_by_user=member,
            event=event,
        )


def _list_queries(account: User) -> int:
    client = Client()
    client.force_login(account)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(HOLDS)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    return len(queries)


def test_a_players_list_costs_the_same_for_few_and_many_holds(
    players: list[Player],
) -> None:
    _self_reported_pair(players, FIRST_DAY)
    few = _list_queries(players[0].account)
    for day in range(1, 6):
        _self_reported_pair(players, FIRST_DAY + timedelta(days=day))

    many = _list_queries(players[0].account)

    assert many == few


def test_an_organizers_list_costs_the_same_for_few_and_many_holds(
    event: Event, member: User
) -> None:
    _event_pair(event, member, FIRST_DAY)
    few = _list_queries(member)
    for day in range(1, 6):
        _event_pair(event, member, FIRST_DAY + timedelta(days=day))

    many = _list_queries(member)

    assert many == few

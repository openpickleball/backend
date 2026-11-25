"""The Organizer's dispute list shows only disputes the member can settle
(the maintainer's decision on #272).

A member whose Player plays in any match of a disputed match's
correction chain cannot settle it (``settles_dispute``), so the list
leaves it out. The rule runs in one database query, so the list's query
count does not grow with its rows. Made-up data only.
"""

from datetime import date
from typing import Any

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
from apps.matches.models import Match, MatchFormat
from apps.matches.permissions import settles_dispute
from apps.matches.services import record_match
from apps.matches.workflow import dispute_match, edit_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

LIST = "/api/v1/me/matches/organizer-disputes"
PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (11, 9))


def _signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _member(event: Event, account: User | None = None) -> User:
    member: User = account or UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return member


def _four() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def _record(players: list[Player], event: Event, submitter: User) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=submitter,
        event=event,
    ).match


def _edit(match: Match, players: list[Player], submitter: User) -> Match:
    return edit_match(
        match,
        actor=submitter,
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
    )


def _codes(account: User) -> list[str]:
    response = _signed_in(account).get(LIST)
    assert response.status_code == 200
    return [item["code"] for item in response.json()["items"]]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def test_a_member_who_plays_on_either_team_does_not_see_the_dispute(
    event: Event,
) -> None:
    free = _member(event)
    players = _four()
    disputed = dispute_match(_record(players, event, free), actor=players[0].account)
    team_a = _member(event, players[1].account)
    team_b = _member(event, players[3].account)

    assert _codes(free) == [disputed.public_code]
    for playing in (team_a, team_b):
        assert not settles_dispute(playing, disputed)
        assert _codes(playing) == []


def test_a_member_who_plays_elsewhere_in_the_chain_does_not_see_the_dispute(
    event: Event,
) -> None:
    free = _member(event)
    first_players, middle_players, last_players = _four(), _four(), _four()
    first = _record(first_players, event, free)
    middle = _edit(first, middle_players, free)
    last = _edit(middle, last_players, free)
    disputed = dispute_match(last, actor=last_players[0].account)
    in_first = _member(event, first_players[2].account)
    in_middle = _member(event, middle_players[0].account)

    assert _codes(free) == [disputed.public_code]
    for conflicted in (in_first, in_middle):
        assert not settles_dispute(conflicted, disputed)
        assert _codes(conflicted) == []


def test_a_member_in_another_chain_still_sees_the_dispute(event: Event) -> None:
    free = _member(event)
    players = _four()
    disputed = dispute_match(_record(players, event, free), actor=players[0].account)
    elsewhere = _four()
    _edit(_record(elsewhere, event, free), _four(), free)
    unrelated = _member(event, elsewhere[0].account)

    assert settles_dispute(unrelated, disputed)
    assert _codes(unrelated) == [disputed.public_code]


def _disputes(event: Event, submitter: User, count: int) -> None:
    """``count`` disputed matches, every other one at the end of a chain."""
    for number in range(count):
        players = _four()
        match = _record(players, event, submitter)
        if number % 2:
            players = _four()
            match = _edit(match, players, submitter)
        dispute_match(match, actor=players[0].account)


def _list_queries(account: User) -> int:
    client = _signed_in(account)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(LIST)
    assert response.status_code == 200
    return len(queries)


def test_the_list_query_count_does_not_grow_with_its_rows(event: Event) -> None:
    free = _member(event)
    _disputes(event, free, 2)
    small = _list_queries(free)

    _disputes(event, free, 6)
    large = _list_queries(free)

    assert len(_codes(free)) == 8
    # Session, Account, the list, its count, and three prefetches.
    assert small == large == 7

"""A match's Event, Organizer, and Club on MatchOut (issue #302).

Every response that shows a match names its Event (OPEN Code and name)
with the Event's Organizer by OPEN Code only, the code a player gives to
report that Organizer, and the match's Club (OPEN Code and name). Both
are null when the match has none. The Organizer's name, the match's
rating format, and row ids never show. Made-up data only.
"""

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from pytest_django import DjangoAssertNumQueries

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.schemas import match_out, with_match_details
from apps.matches.services import record_match
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (11, 9))
# A fixed made-up name, so no Faker Club or Event name can contain it.
ORGANIZER_NAME = "Made-up Organizer Name Zeta"


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _member(event: Event) -> User:
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return member


def _event_with_club() -> Event:
    event: Event = EventFactory.create(
        organizer=verified_organizer(name=ORGANIZER_NAME)
    )
    ClubOrganizerGrantFactory.create(
        club=ClubFactory.create(), organizer=event.organizer
    )
    return event


def _event_match(
    players: list[Player], event: Event | None = None, *, with_club: bool = True
) -> Match:
    event = event or _event_with_club()
    club = None
    if with_club:
        grant = event.organizer.club_grants.first()
        club = grant.club if grant is not None else None
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
        club=club,
    ).match


def _self_reported(players: list[Player]) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def _four() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def _expected_event(match: Match) -> dict[str, Any]:
    assert match.event is not None
    return {
        "code": match.event.public_code,
        "name": match.event.name,
        "organizer": match.event.organizer.public_code,
    }


def _expected_club(match: Match) -> dict[str, Any]:
    assert match.club is not None
    return {"code": match.club.public_code, "name": match.club.name}


def test_get_match_shows_the_event_organizer_code_and_club() -> None:
    players = _four()
    match = _event_match(players)
    assert match.club is not None

    response = signed_in(players[0].account).get(f"/api/v1/matches/{match.public_code}")

    assert response.status_code == 200
    body = response.json()
    assert body["event"] == _expected_event(match)
    assert body["club"] == _expected_club(match)
    assert match.event is not None
    assert match.event.organizer.name == ORGANIZER_NAME
    assert ORGANIZER_NAME not in response.content.decode()
    assert "rating_format" not in body


def test_an_event_match_with_no_club_shows_a_null_club() -> None:
    players = _four()
    match = _event_match(players, with_club=False)

    body = signed_in(players[0].account).get(f"/api/v1/matches/{match.public_code}")

    assert body.json()["event"] == _expected_event(match)
    assert body.json()["club"] is None


def test_a_self_reported_match_shows_no_event_and_no_club() -> None:
    players = _four()
    match = _self_reported(players)

    body = (
        signed_in(players[0].account).get(f"/api/v1/matches/{match.public_code}").json()
    )

    assert body["event"] is None
    assert body["club"] is None


def test_the_organizer_code_is_the_one_a_report_takes() -> None:
    players = _four()
    match = _event_match(players)
    client = signed_in(players[0].account)
    organizer = client.get(f"/api/v1/matches/{match.public_code}").json()["event"][
        "organizer"
    ]

    response = client.post(
        f"/api/v1/organizers/{organizer}/reports",
        {"reason": "wrong_results"},
        content_type="application/json",
    )

    assert response.status_code == 201
    assert response.json()["organizer"] == organizer


# Lists.


def test_the_player_match_list_shows_the_event_and_club() -> None:
    players = _four()
    match = _event_match(players)

    response = signed_in(players[2].account).get(
        f"/api/v1/players/{players[0].public_code}/matches"
    )
    body = response.json()

    assert body["items"][0]["event"] == _expected_event(match)
    assert body["items"][0]["club"] == _expected_club(match)
    assert ORGANIZER_NAME not in response.content.decode()


def test_the_awaiting_confirmation_list_shows_the_event() -> None:
    players = _four()
    match = _event_match(players)
    raw_update(match, status=MatchStatus.PENDING)

    body = (
        signed_in(players[0].account)
        .get("/api/v1/me/matches/awaiting-confirmation")
        .json()
    )

    assert [item["code"] for item in body["items"]] == [match.public_code]
    assert body["items"][0]["event"] == _expected_event(match)
    assert body["items"][0]["club"] == _expected_club(match)


def test_the_dispute_lists_show_the_event() -> None:
    players = _four()
    match = _event_match(players)
    dispute_match(match, actor=players[0].account)
    assert match.event is not None

    mine = signed_in(players[0].account).get("/api/v1/me/matches/disputes").json()
    organizer = (
        signed_in(_member(match.event))
        .get("/api/v1/me/matches/organizer-disputes")
        .json()
    )

    for body in (mine, organizer):
        assert body["items"][0]["event"] == _expected_event(match)
        assert body["items"][0]["club"] == _expected_club(match)


def test_the_holds_list_shows_the_event_under_match() -> None:
    players = _four()
    earlier = _event_match(players)
    record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    )
    assert earlier.event is not None

    body = signed_in(_member(earlier.event)).get("/api/v1/me/matches/holds").json()

    assert [item["match"]["code"] for item in body["items"]] == [earlier.public_code]
    assert body["items"][0]["match"]["event"] == _expected_event(earlier)
    assert body["items"][0]["match"]["club"] == _expected_club(earlier)


# Queries.


def test_match_out_reads_the_event_organizer_and_club_prefetched(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    players = _four()
    match = _event_match(players)
    loaded = with_match_details(Match.objects.filter(pk=match.pk)).get()

    with django_assert_num_queries(0):
        out = match_out(loaded, players[0].account)

    assert out.event is not None
    assert out.event.organizer == loaded.event.organizer.public_code  # type: ignore[union-attr]
    assert out.club is not None


def _list_queries(count: int) -> int:
    viewer = PlayerFactory.create()
    for _ in range(count):
        others = _four()
        _event_match([viewer, *others[:3]])
    client = signed_in(viewer.account)
    with CaptureQueriesContext(connection) as captured:
        response = client.get(f"/api/v1/players/{viewer.public_code}/matches")
    assert response.json()["count"] == count
    return len(captured.captured_queries)


def test_a_page_of_event_matches_takes_the_same_queries_at_any_size() -> None:
    one = _list_queries(1)
    cache.clear()
    four = _list_queries(4)

    assert one == four

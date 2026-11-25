"""The Organizer's dispute operations never act as moderator (#269).

An Account that is both a moderator and a member of the Event's verified
Organizer passes the operations' Organizer check. The operations are for
the Organizer level only (moderators use the admin, ADR 0029), so the
workflow must apply the Organizer's rules to it: a correction still needs
every named Club's consent, and a duplicate's code is shown only under
the normal rule. Made-up data only.
"""

import json
from datetime import date
from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Club, ClubOrganizerGrant
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.workflow import dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (11, 9))
REASON = "Made-up reason for a test."


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _member(event: Event, account: User | None = None) -> User:
    member: User = account or UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    return member


def _record(
    players: list[Player],
    event: Event,
    games: tuple[tuple[int, int], ...] = GAMES,
    club: Club | None = None,
) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
        club=club,
    ).match


def _correct(account: User, match: Match, body: dict[str, Any]) -> Any:
    client = Client()
    client.force_login(account)
    return client.post(
        f"/api/v1/matches/{match.public_code}/dispute-correction",
        json.dumps(body),
        content_type="application/json",
    )


def _body(players: list[Player], games: list[dict[str, int]]) -> dict[str, Any]:
    return {
        "format": "doubles",
        "played_on": PLAYED_ON.isoformat(),
        "team_a": [p.public_code for p in players[:2]],
        "team_b": [p.public_code for p in players[2:]],
        "games": games,
        "reason": REASON,
    }


def test_a_moderator_member_cannot_correct_without_club_consent(
    event: Event, moderator: User
) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    players = [PlayerFactory.create() for _ in range(4)]
    match = _record(players, event, club=club)
    dispute_match(match, actor=players[0].account)
    ClubOrganizerGrant.objects.filter(club=club).delete()
    _member(event, moderator)
    count = Match.objects.count()

    response = _correct(
        moderator,
        match,
        _body(players, [{"team_a": 11, "team_b": 3}, {"team_a": 11, "team_b": 5}]),
    )

    assert response.status_code == 404
    assert Match.objects.get(pk=match.pk).status == MatchStatus.DISPUTED
    assert Match.objects.count() == count


def test_a_moderator_member_sees_a_duplicate_code_only_under_the_normal_rule(
    event: Event, moderator: User
) -> None:
    players = [PlayerFactory.create() for _ in range(4)]
    match = _record(players, event)
    dispute_match(match, actor=players[0].account)
    others = [PlayerFactory.create() for _ in range(4)]
    other_games = ((11, 2), (11, 4))
    _record(others, event, games=other_games)
    _member(event, moderator)

    response = _correct(
        moderator,
        match,
        _body(others, [{"team_a": a, "team_b": b} for a, b in other_games]),
    )

    assert response.status_code == 409
    [item] = response.json()["errors"]
    assert "existing_code" not in item
    assert Match.objects.get(pk=match.pk).status == MatchStatus.DISPUTED

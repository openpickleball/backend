"""A dispute correction keeps the match's scoring type and game target (#269).

A correction that leaves out ``scoring_type`` or ``game_target`` used to
fall back to side-out to 11, so a disputed rally match, or one played to
15, was silently corrected to other settings whenever the scores still
fit. An omitted value now comes from the disputed match. Made-up data
only.
"""

import json
from datetime import date
from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    GameTarget,
    Match,
    MatchFormat,
    ScoringType,
)
from apps.matches.services import record_match
from apps.matches.workflow import dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)


def _member(event: Event) -> User:
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    return member


def _disputed(
    event: Event,
    players: list[Player],
    *,
    scoring_type: str,
    game_target: int,
    games: tuple[tuple[int, int], ...],
) -> Match:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=PLAYED_ON,
        scoring_type=scoring_type,
        game_target=game_target,
        submitted_by_user=_member(event),
        event=event,
    ).match
    return dispute_match(match, actor=players[0].account)


def _correct(
    event: Event, match: Match, players: list[Player], games: list[list[int]]
) -> Any:
    client = Client()
    client.force_login(_member(event))
    body = {
        "format": "doubles",
        "played_on": PLAYED_ON.isoformat(),
        "team_a": [p.public_code for p in players[:2]],
        "team_b": [p.public_code for p in players[2:]],
        "games": [{"team_a": a, "team_b": b} for a, b in games],
        "reason": "Made-up reason for a test.",
    }
    return client.post(
        f"/api/v1/matches/{match.public_code}/dispute-correction",
        json.dumps(body),
        content_type="application/json",
    )


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def test_a_rally_match_stays_rally(event: Event, players: list[Player]) -> None:
    match = _disputed(
        event,
        players,
        scoring_type=ScoringType.RALLY,
        game_target=GameTarget.ELEVEN,
        games=((11, 7), (11, 9)),
    )

    response = _correct(event, match, players, [[11, 3], [11, 5]])

    assert response.status_code == 200
    assert response.json()["scoring_type"] == ScoringType.RALLY
    corrected = Match.objects.get(public_code=response.json()["code"])
    assert corrected.scoring_type == ScoringType.RALLY


def test_a_match_to_15_stays_to_15(event: Event, players: list[Player]) -> None:
    match = _disputed(
        event,
        players,
        scoring_type=ScoringType.SIDE_OUT,
        game_target=GameTarget.FIFTEEN,
        games=((15, 7), (15, 9)),
    )

    # 15-13 also fits a game to 11 (past the target, won by exactly 2).
    response = _correct(event, match, players, [[15, 13], [15, 13]])

    assert response.status_code == 200
    assert response.json()["game_target"] == GameTarget.FIFTEEN
    corrected = Match.objects.get(public_code=response.json()["code"])
    assert corrected.game_target == GameTarget.FIFTEEN

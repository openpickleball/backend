"""A dispute reason with a control character is a 422, never a 500 (#269).

Postgres refuses a NUL in text, so a reason holding one used to reach
``record_event`` and fail there. Control characters and lone surrogates
(Unicode categories Cc and Cs) are refused with fixed text, except the
line breaks and tab a typed reason can hold. Made-up data only.
"""

import json
from datetime import date
from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.workflow import dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

CONTROL_REASONS = ["a\x00b", "a\x07b", "a\x1bb", "a\x7fb"]
MESSAGE = "The reason has a character not allowed."


@pytest.fixture
def setup() -> tuple[Match, list[Player], User]:
    players = [PlayerFactory.create() for _ in range(4)]
    event = EventFactory.create(organizer=verified_organizer())
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    submitter: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=submitter)
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=date(2026, 6, 1),
        submitted_by_user=submitter,
        event=event,
    ).match
    dispute_match(match, actor=players[0].account)
    return match, players, member


def _post(client: Client, url: str, body: dict[str, Any]) -> Any:
    return client.post(url, json.dumps(body), content_type="application/json")


def _assert_refused(response: Any, match: Match, reason: str) -> None:
    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error == {"field": "body.reason", "code": "invalid", "message": MESSAGE}
    assert reason not in response.content.decode()
    assert Match.objects.get(pk=match.pk).status == MatchStatus.DISPUTED
    assert not AuditEvent.objects.filter(
        action__in=[AuditAction.DISPUTE_OUTCOME, AuditAction.MATCH_EDIT],
        target_object_id=str(match.pk),
    ).exists()


@pytest.mark.parametrize("reason", CONTROL_REASONS)
def test_resolve_refuses_a_control_character(
    setup: tuple[Match, list[Player], User], reason: str
) -> None:
    match, _players, member = setup
    client = Client()
    client.force_login(member)

    response = _post(
        client,
        f"/api/v1/matches/{match.public_code}/resolve-dispute",
        {"outcome": "confirmed", "reason": reason},
    )

    _assert_refused(response, match, reason)


@pytest.mark.parametrize("reason", CONTROL_REASONS)
def test_correction_refuses_a_control_character(
    setup: tuple[Match, list[Player], User], reason: str
) -> None:
    match, players, member = setup
    client = Client()
    client.force_login(member)
    count = Match.objects.count()

    response = _post(
        client,
        f"/api/v1/matches/{match.public_code}/dispute-correction",
        {
            "format": "doubles",
            "played_on": "2026-06-01",
            "team_a": [p.public_code for p in players[:2]],
            "team_b": [p.public_code for p in players[2:]],
            "games": [{"team_a": 11, "team_b": 3}, {"team_a": 11, "team_b": 5}],
            "reason": reason,
        },
    )

    _assert_refused(response, match, reason)
    assert Match.objects.count() == count


def test_line_breaks_and_tabs_are_allowed(
    setup: tuple[Match, list[Player], User],
) -> None:
    match, _players, member = setup
    client = Client()
    client.force_login(member)
    reason = "Made-up first line.\r\n\tMade-up second line."

    response = _post(
        client,
        f"/api/v1/matches/{match.public_code}/resolve-dispute",
        {"outcome": "confirmed", "reason": reason},
    )

    assert response.status_code == 200
    [audit] = AuditEvent.objects.filter(
        action=AuditAction.DISPUTE_OUTCOME, target_object_id=str(match.pk)
    )
    assert audit.reason == reason

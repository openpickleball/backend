"""A withdrawal on a voided match is refused, even after an earlier one (#267).

A player who withdrew their dispute, and whose match was voided later,
gets ``MatchStateError`` (409 from the API) on another withdrawal, as
``dispute_match`` refuses a voided match. A repeat on a match that is not
voided still changes nothing. Made-up data only.
"""

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON
from apps.matches.tests.factories import void_for_test
from apps.matches.workflow import (
    MatchStateError,
    dispute_match,
    withdraw_dispute,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db


@pytest.fixture
def member(event: Event) -> User:
    """A member of ``event``'s verified Organizer, with no Player."""
    account: User = OrganizerMemberFactory.create(organizer=event.organizer).account
    return account


@pytest.fixture
def event_confirmed(event: Event, member: User, players: list[Player]) -> Match:
    """A confirmed Event match that ``member`` may settle or void."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=member,
        event=event,
    ).match


def test_a_withdrawal_after_a_void_is_refused(
    event_confirmed: Match, players: list[Player], member: User
) -> None:
    dispute_match(event_confirmed, actor=players[0].account)
    withdraw_dispute(event_confirmed, actor=players[0].account)
    void_for_test(event_confirmed, actor=member)

    with pytest.raises(MatchStateError):
        withdraw_dispute(event_confirmed, actor=players[0].account)


def test_the_api_answers_409_for_a_withdrawal_after_a_void(
    event_confirmed: Match, players: list[Player], member: User
) -> None:
    dispute_match(event_confirmed, actor=players[0].account)
    withdraw_dispute(event_confirmed, actor=players[0].account)
    void_for_test(event_confirmed, actor=member)
    client = Client()
    client.force_login(players[0].account)
    code = event_confirmed.public_code

    response = client.post(f"/api/v1/matches/{code}/withdraw-dispute")

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert Match.objects.get(pk=event_confirmed.pk).status == MatchStatus.VOIDED


def test_a_repeat_on_a_match_not_voided_still_changes_nothing(
    confirmed: Match, players: list[Player]
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    withdraw_dispute(confirmed, actor=players[0].account)

    result = withdraw_dispute(confirmed, actor=players[0].account)

    assert result.status == MatchStatus.CONFIRMED

"""The admin's bulk void refuses a match the moderator played in (#40, #273).

A moderator's only match actions are the bulk voids. A match whose
correction chain holds the moderator's own Player is counted as refused:
it stays as it was, and the message shows only the counts, never which
rule failed. A moderator who did not play voids every match. Made-up data
only.
"""

from datetime import date, timedelta

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.contrib.messages.storage.base import Message
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, SubmissionBatch
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.matches.workflow import dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up moderator reason."
PLAYED_ON = date(2026, 2, 14)
OTHER_RULES = ("Only a moderator", "current status", "nothing to", "reason")


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


@pytest.fixture
def playing_moderator() -> User:
    return _moderator()


@pytest.fixture
def players(playing_moderator: User) -> list[Player]:
    """Player 0 belongs to the moderator."""
    return [
        PlayerFactory.create(account=playing_moderator),
        *(PlayerFactory.create() for _ in range(3)),
    ]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _batch(players: list[Player], event: Event, member: User) -> tuple[Match, Match]:
    """Two confirmed matches of one upload batch: the moderator's Player
    plays in the first only."""
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    others = [PlayerFactory.create() for _ in range(4)]
    own, other = (
        record_match(
            format=MatchFormat.DOUBLES,
            team_a=lineup[:2],
            team_b=lineup[2:],
            games=((11, 7), (11, 9)),
            played_on=PLAYED_ON + timedelta(days=day),
            event=event,
            submitted_by_user=member,
            submission_batch=batch,
        ).match
        for day, lineup in enumerate((players, others))
    )
    return own, other


def _run(client: Client, action: str, match: Match) -> list[Message]:
    response = client.post(
        reverse("admin:matches_match_changelist"),
        {
            "action": action,
            "_selected_action": [str(match.pk)],
            "reason": REASON,
            "post": "yes",
        },
        follow=True,
    )
    assert response.status_code == 200
    return list(get_messages(response.wsgi_request))


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


@pytest.mark.parametrize("action", ["void_batch_selected", "void_submitter_selected"])
@pytest.mark.parametrize("disputed", [False, True])
def test_a_match_they_played_in_is_refused(
    client: Client,
    playing_moderator: User,
    players: list[Player],
    event: Event,
    member: User,
    action: str,
    disputed: bool,
) -> None:
    own, other = _batch(players, event, member)
    if disputed:
        dispute_match(own, actor=players[1].account)
    status = _status(own)
    client.force_login(playing_moderator)

    messages = _run(client, action, other)

    assert [str(m) for m in messages] == [
        "1 match(es) voided, 0 already voided, 1 refused."
    ]
    for wording in OTHER_RULES:
        assert wording not in str(messages[0])
    assert _status(own) == status
    assert _status(other) == MatchStatus.VOIDED
    voids = AuditEvent.objects.filter(action=AuditAction.MATCH_VOID)
    assert [e.target_object_id for e in voids] == [str(other.pk)]


@pytest.mark.parametrize("action", ["void_batch_selected", "void_submitter_selected"])
def test_a_moderator_who_did_not_play_voids_every_match(
    client: Client,
    players: list[Player],
    event: Event,
    member: User,
    action: str,
) -> None:
    own, other = _batch(players, event, member)
    client.force_login(_moderator())

    messages = _run(client, action, own)

    assert [str(m) for m in messages] == [
        "2 match(es) voided, 0 already voided, 0 refused."
    ]
    assert _status(own) == MatchStatus.VOIDED
    assert _status(other) == MatchStatus.VOIDED

"""A participant disputes a match at most once (review finding A, #26).

After the Event's verified Organizer resolves a dispute as confirmed,
the same participant disputing again changes nothing: no status change,
no Audit Event, no signal. Another participant may still dispute.
Made-up data only.
"""

import pytest

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON, SignalLog
from apps.matches.workflow import dispute_match, resolve_dispute
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


def _disputes(match: Match) -> int:
    return AuditEvent.objects.filter(
        action=AuditAction.MATCH_DISPUTE, target_object_id=str(match.pk)
    ).count()


def test_a_repeat_dispute_after_resolution_changes_nothing(
    event_confirmed: Match,
    players: list[Player],
    member: User,
    signals: SignalLog,
) -> None:
    dispute_match(event_confirmed, actor=players[0].account)
    resolve_dispute(
        event_confirmed, actor=member, outcome=MatchStatus.CONFIRMED, reason="checked"
    )
    signals.changed.clear()

    result = dispute_match(event_confirmed, actor=players[0].account)

    assert result.status == MatchStatus.CONFIRMED
    assert Match.objects.get(pk=event_confirmed.pk).status == MatchStatus.CONFIRMED
    assert _disputes(event_confirmed) == 1
    assert signals.changed == []


def test_another_participant_may_still_dispute(
    event_confirmed: Match,
    players: list[Player],
    member: User,
    signals: SignalLog,
) -> None:
    dispute_match(event_confirmed, actor=players[0].account)
    resolve_dispute(
        event_confirmed, actor=member, outcome=MatchStatus.CONFIRMED, reason="checked"
    )

    dispute_match(event_confirmed, actor=players[2].account)

    assert Match.objects.get(pk=event_confirmed.pk).status == MatchStatus.DISPUTED
    assert _disputes(event_confirmed) == 2

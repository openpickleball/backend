"""With no opening row, every withdrawal is refused (#267).

When the latest ``match.dispute`` Audit Event names no Account, or its
Account's Player has no dispute row on the match (only rows written
outside the services), the current period's disputes are unknown. A
player whose dispute was settled in an earlier period must not end the
dispute by withdrawing that stale row: every withdrawal gets
``MatchStateError``, nothing is written, and the match stays disputed
until a resolution or expiry. Made-up data only.
"""

import pytest
from django.db import transaction

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import (
    ConfirmationDecision,
    Match,
    MatchConfirmation,
    MatchFormat,
    MatchStatus,
)
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON, SignalLog
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import (
    MatchStateError,
    dispute_match,
    resolve_dispute,
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


def test_a_stale_dispute_cannot_end_an_unknown_period(
    event_confirmed: Match,
    players: list[Player],
    member: User,
    signals: SignalLog,
) -> None:
    dispute_match(event_confirmed, actor=players[0].account)
    resolve_dispute(
        event_confirmed, actor=member, outcome=MatchStatus.CONFIRMED, reason="checked"
    )
    raw_update(event_confirmed, status=MatchStatus.DISPUTED)
    with transaction.atomic():
        record_event(
            action=AuditAction.MATCH_DISPUTE,
            target=Match.objects.get(pk=event_confirmed.pk),
            before={"status": MatchStatus.CONFIRMED},
            after={"status": MatchStatus.DISPUTED},
        )
    events = AuditEvent.objects.count()
    signals.confirmed.clear()
    signals.changed.clear()

    with pytest.raises(MatchStateError):
        withdraw_dispute(event_confirmed, actor=players[0].account)

    assert Match.objects.get(pk=event_confirmed.pk).status == MatchStatus.DISPUTED
    assert not MatchConfirmation.objects.filter(
        match=event_confirmed, decision=ConfirmationDecision.WITHDRAW
    ).exists()
    assert AuditEvent.objects.count() == events
    assert signals.confirmed == []
    assert signals.changed == []

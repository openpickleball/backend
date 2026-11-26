"""The dispute period does not compare clocks of two hosts (#268, review fix).

``dispute_started_at`` compared dispute rows' times with the resolution
Audit Event's time. A re-dispute written by a host whose clock is behind
the host that resolved the dispute looked older than the resolution, so
the match had no open dispute and stayed disputed forever. The start is
now the time of the match's latest ``match.dispute`` Audit Event, chosen
by row order. Made-up data only.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.conftest import (
    WORKFLOW_GAMES,
    WORKFLOW_PLAYED_ON,
    audit_events_at,
)
from apps.matches.workflow import (
    dispute_match,
    dispute_started_at,
    expire_disputes_and_holds,
    resolve_dispute,
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
    """A confirmed Event match whose disputes ``member`` settles."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=member,
        event=event,
    ).match


@pytest.fixture(autouse=True)
def _default_periods(settings: Settings) -> None:
    settings.MATCH_DISPUTE_EXPIRY_DAYS = 30


def test_a_re_dispute_behind_the_resolvers_clock_still_expires(
    event_confirmed: Match, players: list[Player], member: User
) -> None:
    dispute_match(event_confirmed, actor=players[0].account)
    # The resolving host's clock is an hour ahead.
    with audit_events_at(timezone.now() + timedelta(hours=1)):
        resolve_dispute(
            event_confirmed,
            actor=member,
            outcome=MatchStatus.CONFIRMED,
            reason="Made-up review reason.",
        )
    dispute_match(event_confirmed, actor=players[2].account)
    re_dispute = AuditEvent.objects.filter(action=AuditAction.MATCH_DISPUTE).latest(
        "pk"
    )

    assert dispute_started_at(event_confirmed) == re_dispute.created_at
    assert expire_disputes_and_holds(now=timezone.now() + timedelta(days=29)) == 0
    assert expire_disputes_and_holds(now=timezone.now() + timedelta(days=31)) == 1
    assert Match.objects.get(pk=event_confirmed.pk).status == MatchStatus.VOIDED

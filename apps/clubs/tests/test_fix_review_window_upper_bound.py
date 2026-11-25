"""The review counts only what happened up to ``now`` (#271).

``organizer_review(now=...)`` counts the window from ``now`` minus the
window days to ``now``: an Audit Event or a flag after a past ``now`` is
left out. Made-up data only.
"""

from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.clubs import reports
from apps.clubs.models import Organizer
from apps.clubs.reports import organizer_review
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import FlagReason, Match, MatchFormat
from apps.matches.services import record_match
from apps.matches.workflow import dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _record(players: list[Player], event: Event, games: tuple[int, int]) -> Match:
    member = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=(games, games),
        played_on=date(2026, 5, 1),
        submitted_by_user=member,
        event=event,
    ).match


def _organizer_with_dispute_and_flag() -> Organizer:
    organizer = verified_organizer()
    event: Event = EventFactory.create(organizer=organizer)
    players = [PlayerFactory.create() for _ in range(4)]
    match = _record(players, event, (11, 3))
    # Same teams and date, other scores: a possible duplicate flag.
    _record(players, event, (11, 4))
    dispute_match(match, actor=players[0].account)
    return organizer


def test_events_after_a_past_now_are_left_out() -> None:
    organizer = _organizer_with_dispute_and_flag()

    past = organizer_review(organizer, now=timezone.now() - timedelta(days=1))
    current = organizer_review(organizer)

    assert current.disputes_opened == 1
    assert past.disputes_opened == 0


def test_flags_after_a_past_now_are_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reports, "SIGNAL_FLAG_REASONS", frozenset({FlagReason.POSSIBLE_DUPLICATE})
    )
    organizer = _organizer_with_dispute_and_flag()

    past = organizer_review(organizer, now=timezone.now() - timedelta(days=1))
    current = organizer_review(organizer)

    assert current.signal_flags == 1
    assert past.signal_flags == 0

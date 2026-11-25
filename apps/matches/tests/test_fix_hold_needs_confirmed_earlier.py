"""The hold rule needs a confirmed earlier match (#268, review fix).

The maintainer decided (#268, #265): an open possible duplicate flag
voids its newer match (the flag's ``match``) after the hold period only
when the earlier match (``duplicate_of``) is confirmed. Otherwise a
player's own pending self-submission could void the verified Organizer's
real result just by waiting. Made-up data only.
"""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchFlag, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON, SignalLog
from apps.matches.workflow import (
    confirm_match,
    dispute_match,
    edit_match,
    expire_disputes_and_holds,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db

REASON = "Made-up review reason."
OTHER_GAMES = ((11, 5), (11, 6))


@pytest.fixture(autouse=True)
def _default_periods(settings: Settings) -> None:
    settings.MATCH_DISPUTE_EXPIRY_DAYS = 30
    settings.MATCH_DUPLICATE_HOLD_DAYS = 14


def _days_from_now(days: int) -> datetime:
    return timezone.now() + timedelta(days=days)


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def _expire_events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.MATCH_EXPIRE))


def _counted() -> set[int]:
    return set(Match.objects.counted().values_list("pk", flat=True))


def _near_duplicate(players: list[Player]) -> Match:
    """The same lineup and date as ``pending``, other scores: flagged."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=OTHER_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def _confirm(match: Match, players: list[Player]) -> Match:
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


def test_a_pending_self_submission_never_voids_the_organizers_result(
    pending: Match, players: list[Player], event: Event
) -> None:
    member: User = OrganizerMemberFactory.create(organizer=event.organizer).account
    real = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=OTHER_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        event=event,
        submitted_by_user=member,
    ).match
    assert real.status == MatchStatus.CONFIRMED
    assert MatchFlag.objects.filter(match=real, duplicate_of=pending).exists()

    assert expire_disputes_and_holds(now=_days_from_now(60)) == 0

    assert _status(real) == MatchStatus.CONFIRMED
    assert _expire_events() == []


def test_a_disputed_earlier_match_voids_nothing_until_its_dispute_expires(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    later = _confirm(_near_duplicate(players), players)
    dispute_match(confirmed, actor=players[0].account)

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 0
    assert _status(later) == MatchStatus.CONFIRMED
    assert _status(confirmed) == MatchStatus.DISPUTED

    signals.changed.clear()
    assert expire_disputes_and_holds(now=_days_from_now(31)) == 1

    assert _status(confirmed) == MatchStatus.VOIDED
    assert _status(later) == MatchStatus.CONFIRMED
    assert later.pk in _counted()
    assert signals.changes(later) == [MatchChange.RELEASE]


def _correct(match: Match, players: list[Player], moderator: User) -> Match:
    return edit_match(
        match,
        actor=moderator,
        reason=REASON,
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
    )


def test_a_correction_held_by_a_pending_match_is_not_voided(
    confirmed: Match, players: list[Player], moderator: User
) -> None:
    pending_dup = _near_duplicate(players)
    correction = _correct(confirmed, players, moderator)
    assert MatchFlag.objects.filter(match=correction, duplicate_of=pending_dup).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 0

    assert _status(correction) == MatchStatus.CONFIRMED
    assert _status(pending_dup) == MatchStatus.PENDING


def test_a_correction_held_by_a_confirmed_match_is_the_newer_one(
    confirmed: Match, players: list[Player], moderator: User
) -> None:
    confirmed_dup = _confirm(_near_duplicate(players), players)
    correction = _correct(confirmed, players, moderator)
    assert MatchFlag.objects.filter(
        match=correction, duplicate_of=confirmed_dup
    ).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    assert _status(correction) == MatchStatus.VOIDED
    assert _status(confirmed_dup) == MatchStatus.CONFIRMED
    assert confirmed_dup.pk in _counted()

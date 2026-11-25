"""Expiring one hold leaves the match's other open flags in force (#268).

The hold rule voids a flag's newer match only when its earlier match is
confirmed (ADR 0034). Voiding it ends that flag's hold on the earlier
match, but another open flag can still hold the earlier match, and the
hold against a pending match never ends by this rule. Made-up data only.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchFlag, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import confirm_match, expire_disputes_and_holds
from apps.players.models import Player

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _default_periods(settings: Settings) -> None:
    settings.MATCH_DISPUTE_EXPIRY_DAYS = 30
    settings.MATCH_DUPLICATE_HOLD_DAYS = 14


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def test_a_confirmed_match_held_by_a_pending_one_stays_held_after_a_newer_hold_ends(
    pending: Match, players: list[Player], event: Event
) -> None:
    confirmed_dup = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(confirmed_dup, actor=players[0].account)
    confirm_match(confirmed_dup, actor=players[2].account)
    member: User = OrganizerMemberFactory.create(organizer=event.organizer).account
    organizer_match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 2), (11, 3)),
        played_on=WORKFLOW_PLAYED_ON,
        event=event,
        submitted_by_user=member,
    ).match
    links = set(
        MatchFlag.objects.filter(resolved_at__isnull=True).values_list(
            "match_id", "duplicate_of_id"
        )
    )
    assert links == {
        (confirmed_dup.pk, pending.pk),
        (organizer_match.pk, pending.pk),
        (organizer_match.pk, confirmed_dup.pk),
    }

    assert expire_disputes_and_holds(now=timezone.now() + timedelta(days=15)) == 1

    # The flag against the confirmed match voids the organizer's match.
    assert _status(organizer_match) == MatchStatus.VOIDED
    # The confirmed match's own flag against the pending one stays open
    # and still holds it.
    assert _status(confirmed_dup) == MatchStatus.CONFIRMED
    assert _status(pending) == MatchStatus.PENDING
    assert not Match.objects.counted().filter(pk=confirmed_dup.pk).exists()

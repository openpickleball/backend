"""Dispute expiry after a withdrawal (#267, #268, ADR 0034).

The disputed period runs from the first dispute still open in it. When
the participant who opened the period withdraws and another dispute
stays open, the period runs from that dispute. A match whose last open
dispute was withdrawn is no longer disputed, so it never expires. Made-up
data only.
"""

from datetime import datetime, timedelta

import pytest
from django.db import connection
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.matches.models import (
    ConfirmationDecision,
    Match,
    MatchConfirmation,
    MatchStatus,
)
from apps.matches.tests.conftest import audit_events_at
from apps.matches.workflow import (
    dispute_match,
    dispute_started_at,
    expire_disputes_and_holds,
    withdraw_dispute,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _default_period(settings: Settings) -> None:
    settings.MATCH_DISPUTE_EXPIRY_DAYS = 30


def _days_from_now(days: int) -> datetime:
    return timezone.now() + timedelta(days=days)


def _dispute_row(match: Match, player: Player) -> MatchConfirmation:
    return MatchConfirmation.objects.get(
        match=match, player=player, decision=ConfirmationDecision.DISPUTE
    )


def _backdate(row: MatchConfirmation, when: datetime) -> None:
    """Move one row's time back. Raw SQL: the ORM refuses changes."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE matches_matchconfirmation SET created_at = %s WHERE id = %s",
            [when, row.pk],
        )


def test_the_period_moves_to_the_remaining_dispute(
    confirmed: Match, players: list[Player]
) -> None:
    with audit_events_at(_days_from_now(-40)):
        dispute_match(confirmed, actor=players[0].account)
    dispute_match(confirmed, actor=players[2].account)

    withdraw_dispute(confirmed, actor=players[0].account)

    assert (
        dispute_started_at(confirmed) == _dispute_row(confirmed, players[2]).created_at
    )
    assert expire_disputes_and_holds(now=_days_from_now(1)) == 0
    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.DISPUTED
    assert expire_disputes_and_holds(now=_days_from_now(31)) == 1
    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.VOIDED


def test_the_opening_dispute_still_open_keeps_the_event_time(
    confirmed: Match, players: list[Player]
) -> None:
    long_ago = _days_from_now(-40)
    with audit_events_at(long_ago):
        dispute_match(confirmed, actor=players[0].account)
    dispute_match(confirmed, actor=players[2].account)

    withdraw_dispute(confirmed, actor=players[2].account)

    assert dispute_started_at(confirmed) == long_ago
    assert expire_disputes_and_holds(now=_days_from_now(1)) == 1


def test_a_match_whose_last_dispute_was_withdrawn_is_not_expired(
    confirmed: Match, players: list[Player]
) -> None:
    with audit_events_at(_days_from_now(-40)):
        dispute_match(confirmed, actor=players[0].account)
    withdraw_dispute(confirmed, actor=players[0].account)

    assert dispute_started_at(confirmed) is None
    assert expire_disputes_and_holds(now=_days_from_now(31)) == 0
    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.CONFIRMED


def test_an_old_remaining_dispute_is_a_candidate_with_a_recent_event(
    confirmed: Match, players: list[Player]
) -> None:
    # The event is recent, but the remaining dispute row is older than the
    # cutoff (a host with another clock), so the candidate query must find
    # the match by the row's time too.
    dispute_match(confirmed, actor=players[0].account)
    dispute_match(confirmed, actor=players[2].account)
    _backdate(_dispute_row(confirmed, players[2]), _days_from_now(-40))

    withdraw_dispute(confirmed, actor=players[0].account)

    assert expire_disputes_and_holds(now=_days_from_now(1)) == 1
    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.VOIDED

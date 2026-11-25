"""With no opening row, the dispute period starts at the latest event (#267).

When the latest ``match.dispute`` Audit Event names no Account, or its
Account's Player has no dispute row on the match (only rows written
outside the services), no opening row is known. ``dispute_started_at``
then returns the latest event's time, the later and safe start, so a
dispute row from an earlier settled period never expires the match
early. Made-up data only.
"""

from datetime import datetime, timedelta

import pytest
from django.db import connection, transaction
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event
from apps.matches.models import (
    ConfirmationDecision,
    Match,
    MatchConfirmation,
    MatchStatus,
)
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import (
    dispute_match,
    dispute_started_at,
    expire_disputes_and_holds,
    resolve_dispute,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db


def _backdate_rows(match: Match, when: datetime) -> None:
    """Move every decision row's time back. Raw SQL: the ORM refuses it."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE matches_matchconfirmation SET created_at = %s WHERE match_id = %s",
            [when, match.pk],
        )


def _hand_written_dispute(match: Match) -> AuditEvent:
    """A change into disputed written outside the services, with no actor."""
    raw_update(match, status=MatchStatus.DISPUTED)
    with transaction.atomic():
        return record_event(
            action=AuditAction.MATCH_DISPUTE,
            target=Match.objects.get(pk=match.pk),
            before={"status": MatchStatus.CONFIRMED},
            after={"status": MatchStatus.DISPUTED},
        )


def test_no_opening_row_starts_at_the_latest_event(
    confirmed: Match,
    players: list[Player],
    moderator: User,
    settings: Settings,
) -> None:
    settings.MATCH_DISPUTE_EXPIRY_DAYS = 30
    dispute_match(confirmed, actor=players[0].account)
    resolve_dispute(
        confirmed, actor=moderator, outcome=MatchStatus.CONFIRMED, reason="checked"
    )
    _backdate_rows(confirmed, timezone.now() - timedelta(days=40))
    event = _hand_written_dispute(confirmed)
    assert MatchConfirmation.objects.filter(
        match=confirmed, decision=ConfirmationDecision.DISPUTE
    ).exists()

    assert dispute_started_at(confirmed) == event.created_at
    assert expire_disputes_and_holds(now=timezone.now() + timedelta(days=1)) == 0
    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.DISPUTED

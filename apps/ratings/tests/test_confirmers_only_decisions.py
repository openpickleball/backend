"""``confirmed_by`` is only the players who confirmed (#273). Made-up data only.

Moderators no longer settle single matches, so the adapter no longer
reads the audit log to send every participant of a self-reported match.
A ``dispute.outcome`` Audit Event to confirmed, written here directly as
a historical moderator ruling would have been, changes nothing.
"""

from datetime import date

import pytest
from django.db import transaction

from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import confirm_match, dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.engine_adapter import counted_engine_matches, to_engine_match

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 3, 1)


def test_a_historical_ruling_sends_only_the_confirmers() -> None:
    players: list[Player] = [PlayerFactory.create() for _ in range(4)]
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=[(11, 4)],
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    dispute_match(match, actor=players[2].account)
    raw_update(match, status=MatchStatus.CONFIRMED)
    with transaction.atomic():
        record_event(
            action=AuditAction.DISPUTE_OUTCOME,
            target=match,
            before={"status": MatchStatus.DISPUTED},
            after={"status": MatchStatus.CONFIRMED},
            reason="Made-up historical ruling.",
        )

    fresh = Match.objects.get(pk=match.pk)
    expected = frozenset({str(players[0].public_code)})
    assert to_engine_match(fresh).confirmed_by == expected
    [listed] = counted_engine_matches()
    assert listed.confirmed_by == expected

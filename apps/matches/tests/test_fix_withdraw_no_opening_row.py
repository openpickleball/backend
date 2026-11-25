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
from apps.matches.models import (
    ConfirmationDecision,
    Match,
    MatchConfirmation,
    MatchStatus,
)
from apps.matches.tests.conftest import SignalLog
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import (
    MatchStateError,
    dispute_match,
    resolve_dispute,
    withdraw_dispute,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db


def test_a_stale_dispute_cannot_end_an_unknown_period(
    confirmed: Match, players: list[Player], moderator: User, signals: SignalLog
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    resolve_dispute(
        confirmed, actor=moderator, outcome=MatchStatus.CONFIRMED, reason="checked"
    )
    raw_update(confirmed, status=MatchStatus.DISPUTED)
    with transaction.atomic():
        record_event(
            action=AuditAction.MATCH_DISPUTE,
            target=Match.objects.get(pk=confirmed.pk),
            before={"status": MatchStatus.CONFIRMED},
            after={"status": MatchStatus.DISPUTED},
        )
    events = AuditEvent.objects.count()
    signals.confirmed.clear()
    signals.changed.clear()

    with pytest.raises(MatchStateError):
        withdraw_dispute(confirmed, actor=players[0].account)

    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.DISPUTED
    assert not MatchConfirmation.objects.filter(
        match=confirmed, decision=ConfirmationDecision.WITHDRAW
    ).exists()
    assert AuditEvent.objects.count() == events
    assert signals.confirmed == []
    assert signals.changed == []

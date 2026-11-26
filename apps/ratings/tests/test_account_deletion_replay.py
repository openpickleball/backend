"""Account deletion changes no rating (#116, ADR 0048).

The deleted player's Matches stay in the Ledger with their stored match
type and rating format, so a full replay gives every player, the
deleted one included, the same ratings as before. Made-up data only.
"""

from datetime import date

import pytest

from apps.accounts.deletion import delete_account
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Team
from apps.ratings.engine_adapter import counted_engine_matches, run_replay
from apps.ratings.tests.builders import MIXED, saved_match

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 4, 30)


def test_a_replay_gives_the_same_ratings_before_and_after_a_deletion() -> None:
    event = EventFactory.create(kind=EventKind.TOURNAMENT)
    _, players = saved_match(event=event, divisions=MIXED, played_on=date(2026, 3, 1))
    saved_match(event=event, players=players, played_on=date(2026, 3, 8))
    saved_match(event=event, divisions=MIXED, played_on=date(2026, 3, 15))
    ledger = counted_engine_matches()
    before = run_replay(ledger, AS_OF)

    deleted = players[Team.A][0]
    delete_account(deleted.account, actor=deleted.account)

    assert counted_engine_matches() == ledger
    after = run_replay(counted_engine_matches(), AS_OF)
    assert after.summaries == before.summaries
    assert str(deleted.public_code) in {s.player_id for s in after.summaries}

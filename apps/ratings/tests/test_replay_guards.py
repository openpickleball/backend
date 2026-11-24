"""Guards around replay requests (#47). Made-up data only.

Every ``MatchChange`` must have a ``ReplayReason``: the receiver turns
the change into a reason inside the workflow's transaction, so a change
kind with no reason would roll the workflow action back. A replay whose
requests another run took while it waited for the lock writes nothing.
"""

from datetime import date
from typing import Any

import pytest

from apps.matches.signals import MatchChange
from apps.ratings import services
from apps.ratings.models import (
    EngineSnapshot,
    RatingReplay,
    RatingReplayRequest,
    ReplayReason,
)
from apps.ratings.services import ReplayOutcome, replay_ratings


@pytest.mark.parametrize("change", list(MatchChange))
def test_every_match_change_is_a_replay_reason(change: MatchChange) -> None:
    assert change.value in ReplayReason.values
    assert ReplayReason(change) == change.value


@pytest.mark.django_db
def test_requests_taken_while_waiting_for_the_lock_leave_nothing_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RatingReplayRequest.objects.create(
        earliest_date=date(2026, 3, 1), reason=ReplayReason.VOID
    )
    real_row = services._snapshot_row

    def another_run_took_them(*args: Any, **kwargs: Any) -> Any:
        RatingReplayRequest.objects.all().delete()
        return real_row(*args, **kwargs)

    monkeypatch.setattr(services, "_snapshot_row", another_run_took_them)

    assert replay_ratings() == ReplayOutcome.NOTHING_REQUESTED

    assert not RatingReplay.objects.exists()
    assert not EngineSnapshot.objects.exists()

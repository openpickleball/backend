"""A Match that stops counting while the live update waits writes nothing (#46).

The live update waits for the engine snapshot row's lock. A Match voided
in that time no longer counts once the lock is held, so the update
returns ``not_counted`` and writes nothing, instead of failing in
``apply_rating()``. Made-up data only.
"""

from typing import Any

import pytest

from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import MatchFormat, MatchStatus, Team
from apps.matches.tests.factories import raw_update
from apps.players.models import GenderDivision
from apps.ratings import services
from apps.ratings.models import EngineSnapshot, RatingHistory, RatingState
from apps.ratings.services import update_ratings_for_match
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def test_a_match_voided_while_waiting_for_the_lock_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event: Event = EventFactory.create(kind=EventKind.TOURNAMENT)
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES, event=event
    )
    real_snapshot_row = services._snapshot_row

    def voided_while_waiting(*args: Any, **kwargs: Any) -> EngineSnapshot:
        # Another transaction voids the Match while this one waits.
        raw_update(match, status=MatchStatus.VOIDED)
        return real_snapshot_row(*args, **kwargs)

    monkeypatch.setattr(services, "_snapshot_row", voided_while_waiting)

    outcome = update_ratings_for_match(match.pk)

    assert outcome == "not_counted"
    assert not RatingState.objects.exists()
    assert not RatingHistory.objects.exists()
    assert not any(row.snapshot for row in EngineSnapshot.objects.all())

"""The adapter reads each Match's stored match type (#163, ADR 0035).

A change of an Event's kind after a match is recorded never changes that
match's engine match type; a match recorded afterwards gets the new kind.
Made-up data only.
"""

import open_engine
import pytest

from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match
from apps.ratings.engine_adapter import counted_engine_matches, to_engine_match
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db


def _engine_types() -> dict[str, open_engine.MatchType]:
    return {match.match_id: match.match_type for match in counted_engine_matches()}


def test_a_kind_change_keeps_the_type_of_an_earlier_match() -> None:
    event = EventFactory.create(kind=EventKind.TOURNAMENT)
    earlier, _ = saved_match(event=event)
    Event.objects.filter(pk=event.pk).update(kind=EventKind.LEAGUE)

    fresh = Match.objects.get(pk=earlier.pk)

    assert to_engine_match(fresh).match_type is open_engine.MatchType.TOURNAMENT
    assert _engine_types() == {
        str(earlier.public_code): open_engine.MatchType.TOURNAMENT
    }


def test_a_match_after_the_kind_change_gets_the_new_kind() -> None:
    event = EventFactory.create(kind=EventKind.TOURNAMENT)
    earlier, _ = saved_match(event=event)
    event.kind = EventKind.LEAGUE
    event.save()

    later, _ = saved_match(event=event)

    assert _engine_types() == {
        str(earlier.public_code): open_engine.MatchType.TOURNAMENT,
        str(later.public_code): open_engine.MatchType.LEAGUE,
    }

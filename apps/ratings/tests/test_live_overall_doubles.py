"""A plain overall doubles Match updates overall doubles only (#46).

Made-up data only.
"""

import pytest

from apps.audit.models import AuditAction, AuditEvent
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import RatingFormat, Team
from apps.ratings.models import RatingChangeKind, RatingHistory, RatingState
from apps.ratings.services import LiveUpdateOutcome, update_ratings_for_match
from apps.ratings.tests.builders import MIXED, saved_match

pytestmark = pytest.mark.django_db


def test_an_overall_doubles_match_updates_four_players_in_doubles_only() -> None:
    event: Event = EventFactory.create(kind=EventKind.TOURNAMENT)
    match, players = saved_match(
        divisions=MIXED, event=event, rating_format=RatingFormat.DOUBLES
    )

    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.UPDATED

    everyone = [player for team in Team for player in players[team]]
    assert len(everyone) == 4
    for player in everyone:
        [state] = RatingState.objects.filter(player=player)
        assert state.rating_format == RatingFormat.DOUBLES
        assert state.matches_played == 1
        assert state.provisional
        [history] = RatingHistory.objects.filter(player=player)
        assert history.kind == RatingChangeKind.LIVE_UPDATE
        assert history.match == match
        assert history.rating_format == RatingFormat.DOUBLES
    assert AuditEvent.objects.filter(action=AuditAction.RATING_CHANGE).count() == 4

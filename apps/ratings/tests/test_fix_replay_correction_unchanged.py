"""A correction the replay already counted is not replayed again (#47).

A correction of a Match rated live needs a replay. Once the replay
counted the correction, a live update of the correction (a redelivery or
a late task) returns ``unchanged`` and stores no replay request, instead
of asking for another full replay. Made-up data only.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, Team
from apps.matches.workflow import edit_match
from apps.players.models import GenderDivision
from apps.ratings.models import RatingReplay, RatingReplayRequest
from apps.ratings.services import LiveUpdateOutcome, update_ratings_for_match
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _member(match: Match) -> User:
    """A member of the verified Organizer of ``match``'s Event."""
    assert match.event is not None
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=match.event.organizer, account=account)
    return account


def test_a_replayed_correction_is_unchanged_and_requests_nothing(
    django_capture_on_commit_callbacks: Capture,
) -> None:
    match, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        event=EventFactory.create(
            kind=EventKind.TOURNAMENT, organizer=verified_organizer()
        ),
        played_on=date(2026, 3, 1),
    )
    a, b = players[Team.A][0], players[Team.B][0]
    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.UPDATED

    with django_capture_on_commit_callbacks(execute=True):
        correction = edit_match(
            match,
            actor=_member(match),
            format=MatchFormat.SINGLES,
            team_a=[a],
            team_b=[b],
            games=((11, 4), (11, 6)),
            played_on=date(2026, 3, 2),
        )
    assert RatingReplay.objects.count() == 1
    assert not RatingReplayRequest.objects.exists()

    assert update_ratings_for_match(correction.pk) == LiveUpdateOutcome.UNCHANGED

    assert not RatingReplayRequest.objects.exists()

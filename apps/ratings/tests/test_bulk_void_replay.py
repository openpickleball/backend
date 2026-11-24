"""Ratings after a bulk void equal ratings from before the batch (#205).

A moderator voids every match of a bad upload with
``void_submission_batch``. Each void requests a replay, and the replay of
the remaining counted ledger gives every player the rating they had
before the batch was recorded. Made-up data only.
"""

from datetime import date, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, SubmissionBatch
from apps.matches.services import record_match
from apps.matches.workflow import BulkVoidResult, void_submission_batch
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import RatingReplayRequest, RatingState, ReplayReason
from apps.ratings.services import SUMMARY_FIELDS, ReplayOutcome, replay_ratings

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 3, 31)
FIRST_DAY = date(2026, 2, 1)
REASON = "Made-up reason: a compromised upload."

type Ratings = dict[tuple[int, str], dict[str, Any]]


def _ratings() -> Ratings:
    return {
        (state.player_id, state.rating_format): {
            name: getattr(state, name) for name in SUMMARY_FIELDS
        }
        for state in RatingState.objects.all()
    }


def _replay() -> None:
    RatingReplayRequest.objects.create(
        earliest_date=FIRST_DAY, reason=ReplayReason.VOID
    )
    assert replay_ratings(as_of=AS_OF) == ReplayOutcome.REPLAYED


def _record(
    a: Player,
    b: Player,
    day: int,
    games: tuple[tuple[int, int], ...],
    event: Event,
    member: User,
    batch: SubmissionBatch | None = None,
) -> Match:
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=[a],
        team_b=[b],
        games=games,
        played_on=FIRST_DAY + timedelta(days=day),
        event=event,
        submitted_by_user=member,
        submission_batch=batch,
    ).match


def test_ratings_after_a_bulk_void_equal_ratings_before_the_batch() -> None:
    event: Event = EventFactory.create(
        organizer=verified_organizer(), kind=EventKind.TOURNAMENT
    )
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    moderator: User = UserFactory.create()
    moderator.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    a, b, c, d = (PlayerFactory.create() for _ in range(4))

    _record(a, b, 0, ((11, 7), (11, 9)), event, member)
    _record(c, d, 1, ((11, 4), (8, 11), (11, 6)), event, member)
    _record(a, c, 2, ((9, 11), (11, 5), (11, 8)), event, member)
    _replay()
    before = _ratings()
    assert before

    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    for day, (x, y) in enumerate([(b, a), (d, c), (b, d), (d, a), (b, c)], start=3):
        _record(x, y, day, ((11, 0), (11, 1)), event, member, batch)
    _replay()
    assert _ratings() != before

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=5, skipped=0, refused=0)
    assert RatingReplayRequest.objects.exists()
    assert replay_ratings(as_of=AS_OF) == ReplayOutcome.REPLAYED
    assert _ratings() == before

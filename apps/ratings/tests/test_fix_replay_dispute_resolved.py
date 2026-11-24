"""A dispute resolved as confirmed after a replay rates the Match again (#47).

A Match rated live is disputed, so the replay removes it from the
ratings and the snapshot. A moderator then resolves the dispute as
confirmed. Its old live update history row must not make the live
update return ``unchanged``: the snapshot no longer holds the Match, so
it goes to a replay, and the ratings end equal to a full replay of the
counted ledger. Made-up data only.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.workflow import dispute_match, resolve_dispute
from apps.players.models import GenderDivision
from apps.ratings.engine_adapter import counted_engine_matches, run_replay
from apps.ratings.models import RatingReplay, RatingState
from apps.ratings.services import (
    SUMMARY_FIELDS,
    LiveUpdateOutcome,
    update_ratings_for_match,
)
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


def test_a_dispute_resolved_as_confirmed_after_a_replay_is_rated(
    django_capture_on_commit_callbacks: Capture,
) -> None:
    match, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        event=EventFactory.create(kind=EventKind.TOURNAMENT),
    )
    a = players[Team.A][0]
    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.UPDATED

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(match, actor=a.account)
    # The replay ran and dropped the disputed Match's ratings.
    assert not RatingState.objects.exists()

    with django_capture_on_commit_callbacks(execute=True):
        resolve_dispute(
            Match.objects.get(pk=match.pk),
            actor=_moderator(),
            outcome=MatchStatus.CONFIRMED,
            reason="Made-up reason for a test.",
        )

    latest = RatingReplay.objects.order_by("-pk").first()
    assert latest is not None
    expected = {
        (s.player_id, s.rating_format): s
        for s in run_replay(counted_engine_matches(), latest.as_of).summaries
    }
    stored = {
        (str(s.player.public_code), s.rating_format): s
        for s in RatingState.objects.select_related("player")
    }
    assert len(expected) == 2
    assert set(stored) == set(expected)
    for key, summary in expected.items():
        for name in SUMMARY_FIELDS:
            assert getattr(stored[key], name) == getattr(summary, name), (key, name)

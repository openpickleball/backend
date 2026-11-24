"""Resolving a flag reaches the replay receiver (#40, #47). Made-up data only.

A resolved flag releases the matches it held, and each ``release`` stores a
replay request in the same transaction (``apps.ratings.receivers``), so the
ratings replay counts them again.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest

from apps.accounts.models import User
from apps.matches.models import FlagResolution, Match, MatchFormat
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import confirm_match, resolve_flags
from apps.players.models import Player
from apps.ratings import tasks
from apps.ratings.models import RatingReplayRequest, ReplayReason

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Each queued replay task's arguments, instead of running it."""
    calls: list[dict[str, Any]] = []

    def record(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, **kwargs})

    monkeypatch.setattr(tasks.replay_ratings, "apply_async", record)
    return calls


def test_resolving_a_flag_stores_release_replay_requests(
    confirmed: Match,
    players: list[Player],
    moderator: User,
    django_capture_on_commit_callbacks: Capture,
    queued: list[dict[str, Any]],
) -> None:
    later = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(later, actor=players[0].account)
    confirm_match(later, actor=players[2].account)
    releases = RatingReplayRequest.objects.filter(reason=ReplayReason.RELEASE)
    assert not releases.exists()

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        resolve_flags(
            later,
            actor=moderator,
            outcome=FlagResolution.DISMISSED,
            reason="Made-up review reason.",
        )

    assert releases.count() == 2
    assert {request.earliest_date for request in releases} == {WORKFLOW_PLAYED_ON}
    for callback in callbacks:
        callback()
    assert queued

"""Replay requests and the queued replay task (#47). Made-up data only.

Every ``match_changed`` signal stores one replay request in the same
transaction as the change and, after commit, queues
``apps.ratings.tasks.replay_ratings`` with a countdown, so quick changes
share one replay. So does ``schedule_replay``, where a live update sends
a late match or one the snapshot cannot continue. A rolled back change
stores and queues nothing.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from typing import Any

import pytest
from django.conf import settings
from django.db import transaction

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, Team
from apps.matches.signals import MatchChange, match_changed
from apps.matches.workflow import dispute_match, edit_match, void_match
from apps.players.models import GenderDivision, Player
from apps.ratings import tasks
from apps.ratings.models import RatingReplay, RatingReplayRequest, ReplayReason
from apps.ratings.services import (
    LiveUpdateOutcome,
    ReplayOutcome,
    request_replay,
    schedule_replay,
    update_ratings_for_match,
)
from apps.ratings.tests.builders import saved_match
from apps.ratings.tests.factories import make_state

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _member(match: Match) -> User:
    """A member of the verified Organizer of ``match``'s Event."""
    assert match.event is not None
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=match.event.organizer, account=account)
    return account


def _singles(**kwargs: Any) -> tuple[Match, Player, Player]:
    if "players" not in kwargs:
        kwargs["divisions"] = MENS_SINGLES
    match, players = saved_match(
        match_format=MatchFormat.SINGLES,
        event=EventFactory.create(
            kind=EventKind.TOURNAMENT, organizer=verified_organizer()
        ),
        **kwargs,
    )
    return match, players[Team.A][0], players[Team.B][0]


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Each queued replay task's arguments, instead of running it."""
    calls: list[dict[str, Any]] = []

    def record(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, **kwargs})

    monkeypatch.setattr(tasks.replay_ratings, "apply_async", record)
    return calls


# The receiver


@pytest.mark.parametrize("change", list(MatchChange))
def test_each_match_change_stores_a_request_with_its_reason(
    change: MatchChange,
    django_capture_on_commit_callbacks: Capture,
    queued: list[dict[str, Any]],
) -> None:
    match, _, _ = _singles()

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        match_changed.send(
            sender=Match, match=match, change=change, earliest_date=date(2026, 2, 7)
        )

    request = RatingReplayRequest.objects.get()
    assert request.reason == change.value
    assert request.earliest_date == date(2026, 2, 7)
    assert queued == []
    for callback in callbacks:
        callback()
    assert queued == [{"args": (), "countdown": settings.RATING_REPLAY_DELAY_SECONDS}]


def test_a_void_stores_a_request_and_queues_after_commit(
    django_capture_on_commit_callbacks: Capture,
    queued: list[dict[str, Any]],
) -> None:
    match, _, _ = _singles(played_on=date(2026, 3, 1))

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        void_match(match, actor=_member(match))
        assert queued == []

    [request] = RatingReplayRequest.objects.all()
    assert request.reason == ReplayReason.VOID
    assert request.earliest_date == date(2026, 3, 1)
    for callback in callbacks:
        callback()
    assert len(queued) == 1


def test_a_dispute_stores_a_request(
    django_capture_on_commit_callbacks: Capture,
    queued: list[dict[str, Any]],
) -> None:
    match, a, _ = _singles()

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(match, actor=a.account)

    assert RatingReplayRequest.objects.get().reason == ReplayReason.DISPUTE
    assert len(queued) == 1


def test_a_rolled_back_change_stores_and_queues_nothing(
    django_capture_on_commit_callbacks: Capture,
    queued: list[dict[str, Any]],
) -> None:
    match, _, _ = _singles()

    class Abort(Exception):
        pass

    def void_then_abort() -> None:
        with transaction.atomic():
            void_match(match, actor=_member(match))
            raise Abort

    with (
        django_capture_on_commit_callbacks(execute=True) as callbacks,
        pytest.raises(Abort),
    ):
        void_then_abort()

    assert callbacks == []
    assert queued == []
    assert not RatingReplayRequest.objects.exists()


# Coalescing


def test_two_quick_edits_cause_one_replay(
    django_capture_on_commit_callbacks: Capture,
) -> None:
    first, a, b = _singles(played_on=date(2026, 3, 1))
    second, _, _ = _singles(
        players={Team.A: [a], Team.B: [b]}, played_on=date(2026, 3, 8)
    )

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        for match, day in ((first, date(2026, 3, 2)), (second, date(2026, 3, 9))):
            edit_match(
                match,
                actor=_member(match),
                format=MatchFormat.SINGLES,
                team_a=[a],
                team_b=[b],
                games=((11, 4), (11, 6)),
                played_on=day,
            )

    assert RatingReplayRequest.objects.count() == 2
    # A functools.partial callback (the recorded match notice) has no
    # __qualname__.
    replay_callbacks = [
        c for c in callbacks if "replay" in getattr(c, "__qualname__", "")
    ]
    assert len(replay_callbacks) == 2
    for callback in callbacks:
        callback()

    assert RatingReplay.objects.count() == 1
    assert not RatingReplayRequest.objects.exists()


def test_a_redelivered_replay_task_writes_nothing() -> None:
    _singles()
    request_replay(date(2026, 3, 1), ReplayReason.VOID)
    assert tasks.replay_ratings.delay().get() == ReplayOutcome.REPLAYED.value

    assert tasks.replay_ratings.delay().get() == ReplayOutcome.NOTHING_REQUESTED.value

    assert RatingReplay.objects.count() == 1


# schedule_replay and the live update


def test_schedule_replay_stores_a_request(
    django_capture_on_commit_callbacks: Capture,
    queued: list[dict[str, Any]],
) -> None:
    _, a, b = _singles()

    with django_capture_on_commit_callbacks(execute=True):
        schedule_replay([a, b], date(2026, 2, 14))

    request = RatingReplayRequest.objects.get()
    assert request.reason == ReplayReason.LATE
    assert request.earliest_date == date(2026, 2, 14)
    assert queued == [{"args": (), "countdown": settings.RATING_REPLAY_DELAY_SECONDS}]


def test_schedule_replay_twice_causes_one_replay(
    django_capture_on_commit_callbacks: Capture,
) -> None:
    _, a, b = _singles()

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        schedule_replay([a, b], date(2026, 3, 1))
        schedule_replay([a, b], date(2026, 3, 1))
    for callback in callbacks:
        callback()

    assert RatingReplay.objects.count() == 1
    assert not RatingReplayRequest.objects.exists()


def test_a_late_live_update_stores_a_late_request(
    queued: list[dict[str, Any]],
) -> None:
    later, a, b = _singles(played_on=date(2026, 3, 8))
    assert update_ratings_for_match(later.pk) == LiveUpdateOutcome.UPDATED
    earlier, _, _ = _singles(
        players={Team.A: [a], Team.B: [b]}, played_on=date(2026, 3, 1)
    )

    assert update_ratings_for_match(earlier.pk) == LiveUpdateOutcome.LATE

    request = RatingReplayRequest.objects.get()
    assert request.reason == ReplayReason.LATE
    assert request.earliest_date == date(2026, 3, 1)


def test_a_live_update_the_snapshot_cannot_continue_requests_a_replay(
    queued: list[dict[str, Any]],
) -> None:
    match, a, _ = _singles()
    make_state(a, rating_format="singles", parameter_fingerprint="another-set")

    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.REPLAY_NEEDED

    assert RatingReplayRequest.objects.get().earliest_date == match.played_on


def test_a_broker_error_is_logged_by_class_and_does_not_fail_the_change(
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    match, _, _ = _singles()

    def broker_down(*args: object, **kwargs: object) -> None:
        raise ConnectionError("made-up broker outage")

    monkeypatch.setattr(tasks.replay_ratings, "apply_async", broker_down)

    with django_capture_on_commit_callbacks(execute=True):
        void_match(match, actor=_member(match))

    assert RatingReplayRequest.objects.count() == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    [record] = errors
    message = record.getMessage()
    assert "ConnectionError" in message
    assert "made-up broker outage" not in message
    assert record.exc_info is None

"""A live update that is lost still leads to a replay (#47). Made-up data only.

A confirmed Match whose live update task could not be queued (a broker
outage), or failed in a worker (an error, or a time limit while it waited
behind a long replay), stores a replay request, so the next replay or
the 15-minute beat entry rates it. The request holds only a reason and
the Match's date.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest
from django.utils import timezone

from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.workflow import confirm_match
from apps.players.models import GenderDivision
from apps.ratings import services, tasks
from apps.ratings.models import RatingReplayRequest, ReplayReason
from apps.ratings.tests.builders import PLAYED_ON, saved_match

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}
SECRET = "made-up secret detail"


class MadeUpError(Exception):
    pass


def _fail(*args: Any, **kwargs: Any) -> Any:
    raise MadeUpError(SECRET)


@pytest.fixture
def replays_queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tasks.replay_ratings, "apply_async", lambda *a, **k: calls.append(k)
    )
    return calls


def _run_in_worker(match_id: int) -> Any:
    task = tasks.update_ratings_for_match
    task.push_request(is_eager=False)
    try:
        return task.run(match_id)
    finally:
        task.pop_request()


def test_a_live_update_the_broker_refused_stores_a_replay_request(
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    replays_queued: list[dict[str, Any]],
) -> None:
    match, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        status=MatchStatus.PENDING,
    )

    def broker_down(*args: object) -> None:
        raise ConnectionError("made-up broker outage")

    monkeypatch.setattr(tasks.update_ratings_for_match, "delay", broker_down)

    with django_capture_on_commit_callbacks(execute=True):
        confirm_match(match, actor=players[Team.A][0].account)
        confirm_match(Match.objects.get(pk=match.pk), actor=players[Team.B][0].account)

    request = RatingReplayRequest.objects.get()
    assert request.reason == ReplayReason.REPLAY_NEEDED
    assert request.earliest_date == PLAYED_ON
    assert len(replays_queued) == 1


def test_a_live_update_that_failed_in_a_worker_stores_a_replay_request(
    monkeypatch: pytest.MonkeyPatch,
    replays_queued: list[dict[str, Any]],
) -> None:
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        event=EventFactory.create(kind=EventKind.TOURNAMENT),
    )
    monkeypatch.setattr(services, "update_ratings_for_match", _fail)

    with pytest.raises(services.LiveUpdateFailed):
        _run_in_worker(match.pk)

    request = RatingReplayRequest.objects.get()
    assert request.reason == ReplayReason.REPLAY_NEEDED
    assert request.earliest_date == PLAYED_ON


def test_a_failed_live_update_of_a_missing_match_requests_from_today(
    monkeypatch: pytest.MonkeyPatch,
    replays_queued: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(services, "update_ratings_for_match", _fail)

    with pytest.raises(services.LiveUpdateFailed):
        _run_in_worker(987654321)

    request = RatingReplayRequest.objects.get()
    assert request.earliest_date == timezone.now().date()


def test_a_request_that_cannot_be_stored_still_raises_the_fixed_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        event=EventFactory.create(kind=EventKind.TOURNAMENT),
    )
    monkeypatch.setattr(services, "update_ratings_for_match", _fail)
    monkeypatch.setattr(services, "request_replay", _fail)

    with (
        caplog.at_level(logging.INFO, logger="apps.ratings"),
        pytest.raises(services.LiveUpdateFailed),
    ):
        _run_in_worker(match.pk)

    assert not RatingReplayRequest.objects.exists()
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert record.exc_info is None
    assert len([r for r in caplog.records if r.name == "apps.ratings.tasks"]) == 2

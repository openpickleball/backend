"""The replay task and its beat entry (#47). Made-up data only.

``apps.ratings.tasks.replay_ratings`` runs ``services.replay_ratings``
and returns only the outcome's value. In a worker an error is logged by
class once and replaced by a fixed-text ``ReplayFailed``; in eager mode
the original error propagates. A beat entry runs it every 15 minutes, so
a request whose queued task was lost still gets its replay.
"""

import logging
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from celery.schedules import crontab
from django.conf import settings

from apps.ratings import services, tasks
from apps.ratings.models import RatingReplay, ReplayReason
from apps.ratings.services import ReplayFailed, request_replay
from config.celery import app as celery_app

TASK_NAME = "apps.ratings.tasks.replay_ratings"
SECRET = "made-up secret detail"


class MadeUpError(Exception):
    pass


def _fail(*args: Any, **kwargs: Any) -> Any:
    raise MadeUpError(SECRET)


def _run(*, is_eager: bool) -> Any:
    task = tasks.replay_ratings
    task.push_request(is_eager=is_eager)
    try:
        return task.run()
    finally:
        task.pop_request()


@pytest.fixture
def ratings_logs(
    caplog: pytest.LogCaptureFixture,
) -> Iterator[pytest.LogCaptureFixture]:
    with caplog.at_level(logging.INFO, logger="apps.ratings"):
        yield caplog


def test_the_task_has_its_explicit_name() -> None:
    assert tasks.replay_ratings.name == TASK_NAME


def test_the_task_routes_to_the_ratings_queue() -> None:
    route = celery_app.amqp.router.route({}, TASK_NAME)
    assert route["queue"].name == "ratings"


def test_the_task_limits_stay_under_the_visibility_timeout() -> None:
    assert tasks.replay_ratings.soft_time_limit == 1500
    assert tasks.replay_ratings.time_limit == 1800


def test_the_delay_stays_well_under_the_visibility_timeout() -> None:
    assert 0 < settings.RATING_REPLAY_DELAY_SECONDS <= 600


def test_the_beat_entry_runs_the_task_every_15_minutes() -> None:
    entry = settings.CELERY_BEAT_SCHEDULE["replay-ratings"]
    assert entry["task"] == TASK_NAME
    assert entry["schedule"] == crontab(minute="*/15")
    assert entry["options"] == {"expire_seconds": 900}


@pytest.mark.django_db
def test_the_task_returns_the_outcome_only() -> None:
    assert tasks.replay_ratings.delay().get() == "nothing_requested"
    request_replay(date(2026, 3, 1), ReplayReason.VOID)
    assert tasks.replay_ratings.delay().get() == "replayed"
    assert RatingReplay.objects.count() == 1


@pytest.mark.django_db
def test_a_worker_failure_raises_fixed_text_only(
    monkeypatch: pytest.MonkeyPatch, ratings_logs: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(services, "replay_ratings", _fail)

    with pytest.raises(ReplayFailed) as caught:
        _run(is_eager=False)

    error = caught.value
    assert SECRET not in str(error)
    assert "MadeUpError" in str(error)
    assert error.__suppress_context__ is True
    assert error.__cause__ is None
    for record in ratings_logs.records:
        assert SECRET not in record.getMessage()
        assert record.exc_info is None
    [line] = [r for r in ratings_logs.records if r.name == "apps.ratings.tasks"]
    assert "MadeUpError" in line.getMessage()


@pytest.mark.django_db
def test_an_eager_failure_raises_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "replay_ratings", _fail)

    with pytest.raises(MadeUpError, match=SECRET):
        _run(is_eager=True)


@pytest.mark.django_db
def test_a_worker_run_that_succeeds_returns_the_outcome() -> None:
    assert _run(is_eager=False) == "nothing_requested"

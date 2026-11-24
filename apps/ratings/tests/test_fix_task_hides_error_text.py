"""In a worker, the live update task never lets an error's text reach the log (#46).

Celery's worker log prints a failed task's exception and traceback, and an
engine, config, or database error's text can quote values. In a worker
(not eager), the task logs the Match id and the error class once and
raises a fixed-text ``LiveUpdateFailed`` with no context. In eager mode
(tests, local development) the original error propagates. Made-up data
only.
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, Team
from apps.players.models import GenderDivision
from apps.ratings import services, tasks
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}
SECRET = "made-up secret detail"


class MadeUpError(Exception):
    pass


def _match() -> Match:
    event: Event = EventFactory.create(kind=EventKind.TOURNAMENT)
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES, event=event
    )
    return match


def _fail(*args: Any, **kwargs: Any) -> Any:
    raise MadeUpError(SECRET)


def _run(match_id: int, *, is_eager: bool) -> Any:
    task = tasks.update_ratings_for_match
    task.push_request(is_eager=is_eager)
    try:
        return task.run(match_id)
    finally:
        task.pop_request()


@pytest.fixture
def ratings_logs(
    caplog: pytest.LogCaptureFixture,
) -> Iterator[pytest.LogCaptureFixture]:
    with caplog.at_level(logging.INFO, logger="apps.ratings"):
        yield caplog


@pytest.mark.parametrize("step", ["live_config", "_record_and_apply"])
def test_a_worker_failure_raises_fixed_text_only(
    monkeypatch: pytest.MonkeyPatch,
    ratings_logs: pytest.LogCaptureFixture,
    step: str,
) -> None:
    match = _match()
    monkeypatch.setattr(services, step, _fail)

    with pytest.raises(services.LiveUpdateFailed) as caught:
        _run(match.pk, is_eager=False)

    error = caught.value
    assert SECRET not in str(error)
    assert str(match.pk) in str(error)
    assert "MadeUpError" in str(error)
    assert error.__suppress_context__ is True
    assert error.__cause__ is None
    for record in ratings_logs.records:
        assert SECRET not in record.getMessage()
        assert record.exc_info is None
    task_lines = [r for r in ratings_logs.records if r.name == "apps.ratings.tasks"]
    assert len(task_lines) == 1
    assert str(match.pk) in task_lines[0].getMessage()
    assert "MadeUpError" in task_lines[0].getMessage()


def test_an_eager_failure_raises_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _match()
    monkeypatch.setattr(services, "live_config", _fail)

    with pytest.raises(MadeUpError, match=SECRET):
        _run(match.pk, is_eager=True)


def test_a_worker_run_that_succeeds_returns_the_outcome() -> None:
    match = _match()
    assert _run(match.pk, is_eager=False) == "updated"

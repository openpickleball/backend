"""The live rating update task and the receiver that queues it (#46).

Confirming a Match (``match_confirmed``) queues
``apps.ratings.tasks.update_ratings_for_match`` with the Match's row id
only, after the transaction commits, on the ``ratings`` queue. Tasks run
eagerly in tests. Made-up data only.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest
from django.db import transaction

from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.workflow import confirm_match
from apps.players.models import GenderDivision, Player
from apps.ratings import tasks
from apps.ratings.models import RatingHistory, RatingState
from apps.ratings.tests.builders import saved_match
from config.celery import app as celery_app

TASK_NAME = "apps.ratings.tasks.update_ratings_for_match"
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]


def _pending() -> tuple[Match, dict[Team, list[Player]]]:
    # Self-reported: it counts once one player from each team confirms.
    return saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        status=MatchStatus.PENDING,
    )


def _confirm_by_both(match: Match, players: dict[Team, list[Player]]) -> None:
    confirm_match(match, actor=players[Team.A][0].account)
    confirm_match(match, actor=players[Team.B][0].account)


def test_the_task_has_its_explicit_name() -> None:
    assert tasks.update_ratings_for_match.name == TASK_NAME


def test_the_task_routes_to_the_ratings_queue() -> None:
    route = celery_app.amqp.router.route({}, TASK_NAME)
    assert route["queue"].name == "ratings"


def test_the_task_limits_stay_under_the_visibility_timeout() -> None:
    task = tasks.update_ratings_for_match
    assert task.soft_time_limit == 240
    assert task.time_limit == 300


@pytest.mark.django_db
def test_the_task_returns_the_outcome_only() -> None:
    match, _ = saved_match(match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES)
    # Self-reported with no confirmations: the engine does not count it.
    assert tasks.update_ratings_for_match.delay(match.pk).get() == "rejected"
    assert tasks.update_ratings_for_match.delay(987654321).get() == "missing"


@pytest.mark.django_db
def test_confirming_a_match_runs_the_rating_update(
    django_capture_on_commit_callbacks: Capture,
) -> None:
    match, players = _pending()

    with django_capture_on_commit_callbacks(execute=True):
        _confirm_by_both(match, players)

    for team in Team:
        player = players[team][0]
        state = RatingState.objects.get(player=player)
        assert state.rating_format == "singles"
        assert state.provisional
        assert RatingHistory.objects.filter(player=player, match=match).count() == 1


@pytest.mark.django_db
def test_the_task_is_queued_with_the_match_id_only(
    django_capture_on_commit_callbacks: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    match, players = _pending()
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        tasks.update_ratings_for_match,
        "delay",
        lambda *args, **kwargs: queued.append((*args, *kwargs.items())),
    )

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        _confirm_by_both(match, players)
        assert queued == []

    assert queued == []
    for callback in callbacks:
        callback()
    assert queued == [(match.pk,)]


@pytest.mark.django_db
def test_a_rolled_back_confirm_queues_nothing(
    django_capture_on_commit_callbacks: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    match, players = _pending()
    queued: list[object] = []
    monkeypatch.setattr(
        tasks.update_ratings_for_match, "delay", lambda *args: queued.append(args)
    )

    class Abort(Exception):
        pass

    def confirm_then_abort() -> None:
        with transaction.atomic():
            _confirm_by_both(match, players)
            raise Abort

    with (
        django_capture_on_commit_callbacks(execute=True) as callbacks,
        pytest.raises(Abort),
    ):
        confirm_then_abort()

    assert callbacks == []
    assert queued == []
    assert not RatingState.objects.exists()


@pytest.mark.django_db
def test_a_failure_to_queue_does_not_fail_the_confirm(
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    match, players = _pending()

    def broker_down(*args: object) -> None:
        raise ConnectionError("made-up broker outage")

    monkeypatch.setattr(tasks.update_ratings_for_match, "delay", broker_down)

    with django_capture_on_commit_callbacks(execute=True):
        _confirm_by_both(match, players)

    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    [record] = [r for r in caplog.records if r.name == "apps.ratings.receivers"]
    message = record.getMessage()
    assert str(match.pk) in message
    assert "ConnectionError" in message
    assert "made-up broker outage" not in message

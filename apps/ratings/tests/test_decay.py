"""Scheduled reliability decay (#52, ADR 0060). Made-up data only.

The engine measures inactivity at a replay's date: a player who has not
played loses reliability as scheduled replays move forward. So
``decay_reliability()`` computes nothing. It stores a ``scheduled``
replay request when the most recently created replay is at least the
engine config's ``replay_interval_days`` old, and the replay task does
the rest. A beat entry runs the task daily.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from celery.schedules import crontab
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django_celery_beat import schedulers
from django_celery_beat.models import PeriodicTask
from django_celery_beat.schedulers import DatabaseScheduler

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import verified_organizer
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.workflow import confirm_match
from apps.players.models import GenderDivision, Player
from apps.ratings import engine_adapter, services, tasks
from apps.ratings.models import (
    RatingChangeKind,
    RatingHistory,
    RatingReplay,
    RatingReplayRequest,
    RatingState,
    ReplayReason,
)
from apps.ratings.services import DecayOutcome, ReplayFailed, decay_reliability
from apps.ratings.tests.builders import saved_match
from config.celery import app as celery_app

pytestmark = pytest.mark.django_db

TASK_NAME = "apps.ratings.tasks.decay_reliability"
PLAYED_ON = date(2026, 3, 1)
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}
SECRET = "made-up secret detail"

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]


class MadeUpError(Exception):
    pass


class Clock:
    """A frozen UTC clock that a test moves by whole days."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.moment = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(timezone, "now", lambda: self.moment)

    def set_days_after_play(self, days: int) -> None:
        start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        self.moment = start + timedelta(days=days)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


@pytest.fixture
def ratings_logs(
    caplog: pytest.LogCaptureFixture,
) -> Iterator[pytest.LogCaptureFixture]:
    with caplog.at_level(logging.INFO, logger="apps.ratings"):
        yield caplog


def _singles_match() -> tuple[Match, Player, Player]:
    event = EventFactory.create(
        kind=EventKind.TOURNAMENT, organizer=verified_organizer()
    )
    match, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        event=event,
        played_on=PLAYED_ON,
    )
    return match, players[Team.A][0], players[Team.B][0]


def _replayed_ratings() -> tuple[Player, Player]:
    """Two players rated by a replay as of the day they played."""
    _, a, b = _singles_match()
    RatingReplayRequest.objects.create(
        earliest_date=PLAYED_ON, reason=ReplayReason.VOID
    )
    assert services.replay_ratings(as_of=PLAYED_ON) == "replayed"
    return a, b


def _counts() -> tuple[int, int, int, int]:
    return (
        RatingReplayRequest.objects.count(),
        RatingReplay.objects.count(),
        RatingHistory.objects.count(),
        AuditEvent.objects.filter(action=AuditAction.RATING_CHANGE).count(),
    )


# The engine's cadence


def test_the_interval_comes_from_the_engine_config() -> None:
    interval = engine_adapter.replay_interval_days()
    assert interval == engine_adapter.load_config().replay_interval_days
    assert interval == 7


# Outcomes


def test_with_no_rating_nothing_is_requested(clock: Clock) -> None:
    clock.set_days_after_play(30)

    assert decay_reliability() == DecayOutcome.NO_RATINGS
    assert _counts() == (0, 0, 0, 0)


def test_a_replay_six_days_old_is_not_due(clock: Clock) -> None:
    _replayed_ratings()
    before = _counts()
    clock.set_days_after_play(6)

    assert decay_reliability() == DecayOutcome.NOT_DUE
    assert _counts() == before


def test_a_replay_seven_days_old_is_due(clock: Clock) -> None:
    _replayed_ratings()
    clock.set_days_after_play(7)

    assert decay_reliability() == DecayOutcome.REQUESTED
    [request] = RatingReplayRequest.objects.all()
    assert request.reason == ReplayReason.SCHEDULED
    assert request.earliest_date == date(2026, 3, 8)


def test_ratings_with_no_replay_yet_are_due(
    clock: Clock, django_capture_on_commit_callbacks: Capture
) -> None:
    # A live update rates the players; no replay has run.
    match, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        status=MatchStatus.PENDING,
    )
    with django_capture_on_commit_callbacks(execute=True):
        confirm_match(match, actor=players[Team.A][0].account)
        confirm_match(match, actor=players[Team.B][0].account)
    assert RatingState.objects.exists()
    assert not RatingReplay.objects.exists()

    assert decay_reliability() == DecayOutcome.REQUESTED
    [request] = RatingReplayRequest.objects.all()
    assert request.reason == ReplayReason.SCHEDULED


def test_a_pending_scheduled_request_is_not_stored_twice(clock: Clock) -> None:
    _replayed_ratings()
    clock.set_days_after_play(7)
    assert decay_reliability() == DecayOutcome.REQUESTED

    assert decay_reliability() == DecayOutcome.ALREADY_REQUESTED
    assert RatingReplayRequest.objects.count() == 1


def test_a_pending_request_for_another_reason_does_not_block_it(
    clock: Clock,
) -> None:
    _replayed_ratings()
    clock.set_days_after_play(7)
    RatingReplayRequest.objects.create(
        earliest_date=PLAYED_ON, reason=ReplayReason.VOID
    )

    assert decay_reliability() == DecayOutcome.REQUESTED
    reasons = set(RatingReplayRequest.objects.values_list("reason", flat=True))
    assert reasons == {ReplayReason.VOID, ReplayReason.SCHEDULED}


def test_the_latest_replay_decides(clock: Clock) -> None:
    _replayed_ratings()
    # A later replay, as of day 5, makes the decay not due until day 12.
    RatingReplayRequest.objects.create(
        earliest_date=PLAYED_ON, reason=ReplayReason.VOID
    )
    assert services.replay_ratings(as_of=date(2026, 3, 6)) == "replayed"
    clock.set_days_after_play(7)

    assert decay_reliability() == DecayOutcome.NOT_DUE


def test_the_most_recently_created_replay_decides_not_the_latest_as_of(
    clock: Clock,
) -> None:
    _singles_match()
    # A replay as of day 7, then one written later as of day 0 (as a
    # recompute with an earlier --as-of would). The stored ratings come
    # from the later one, so the decay is due on day 7.
    for as_of in (date(2026, 3, 8), PLAYED_ON):
        RatingReplayRequest.objects.create(
            earliest_date=PLAYED_ON, reason=ReplayReason.VOID
        )
        assert services.replay_ratings(as_of=as_of) == "replayed"
    clock.set_days_after_play(7)

    assert decay_reliability() == DecayOutcome.REQUESTED


# Decay through the replay


def test_an_inactive_player_loses_reliability(
    clock: Clock, django_capture_on_commit_callbacks: Capture
) -> None:
    a, b = _replayed_ratings()
    stored = {
        state.player_id: (state.reliability, state.rd)
        for state in RatingState.objects.all()
    }
    replays = RatingReplay.objects.count()
    clock.set_days_after_play(7)

    with django_capture_on_commit_callbacks(execute=True):
        assert tasks.decay_reliability.delay().get() == "requested"

    assert RatingReplay.objects.count() == replays + 1
    replay = RatingReplay.objects.order_by("-created_at", "-id").first()
    assert replay is not None
    assert replay.as_of == date(2026, 3, 8)
    assert not RatingReplayRequest.objects.exists()
    for player in (a, b):
        state = RatingState.objects.get(player=player)
        old_reliability, old_rd = stored[player.pk]
        assert state.reliability < old_reliability
        assert state.rd > old_rd
        assert state.calculated_as_of == date(2026, 3, 8)


def test_each_changed_rating_writes_one_history_row_and_audit_event(
    clock: Clock, django_capture_on_commit_callbacks: Capture
) -> None:
    _replayed_ratings()
    events_before = AuditEvent.objects.filter(action=AuditAction.RATING_CHANGE).count()
    clock.set_days_after_play(7)

    with django_capture_on_commit_callbacks(execute=True):
        decay_reliability()

    replay = RatingReplay.objects.order_by("-created_at", "-id").first()
    assert replay is not None
    rows = list(RatingHistory.objects.filter(replay=replay))
    assert len(rows) == RatingState.objects.count() == 2
    assert {row.kind for row in rows} == {RatingChangeKind.REPLAY}
    events = AuditEvent.objects.filter(action=AuditAction.RATING_CHANGE).count()
    assert events - events_before == len(rows)


def test_a_second_run_on_the_same_day_changes_nothing(
    clock: Clock, django_capture_on_commit_callbacks: Capture
) -> None:
    _replayed_ratings()
    clock.set_days_after_play(7)
    with django_capture_on_commit_callbacks(execute=True):
        assert decay_reliability() == DecayOutcome.REQUESTED
    after_first = _counts()

    with django_capture_on_commit_callbacks(execute=True):
        assert decay_reliability() == DecayOutcome.NOT_DUE

    assert _counts() == after_first


def test_running_the_task_twice_on_the_same_day_changes_nothing(
    clock: Clock, django_capture_on_commit_callbacks: Capture
) -> None:
    _replayed_ratings()
    clock.set_days_after_play(7)
    with django_capture_on_commit_callbacks(execute=True):
        assert tasks.decay_reliability.delay().get() == "requested"
    after_first = _counts()
    assert after_first[0] == 0  # the replay took the request

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        assert tasks.decay_reliability.delay().get() == "not_due"

    assert callbacks == []
    assert _counts() == after_first


def test_the_log_holds_dates_and_no_player(
    clock: Clock, ratings_logs: pytest.LogCaptureFixture
) -> None:
    a, b = _replayed_ratings()
    clock.set_days_after_play(7)
    ratings_logs.clear()

    decay_reliability()

    lines = [
        record.getMessage()
        for record in ratings_logs.records
        if record.name == "apps.ratings.services"
    ]
    assert lines
    text = " ".join(lines)
    assert "requested" in text
    assert "2026-03-08" in text
    assert "2026-03-01" in text
    for player in (a, b):
        assert str(player.public_code) not in text


# The task and its beat entry


def _run(*, is_eager: bool) -> Any:
    task = tasks.decay_reliability
    task.push_request(is_eager=is_eager)
    try:
        return task.run()
    finally:
        task.pop_request()


def _fail(*args: Any, **kwargs: Any) -> Any:
    raise MadeUpError(SECRET)


def test_the_task_has_its_explicit_name() -> None:
    assert tasks.decay_reliability.name == TASK_NAME


def test_the_task_routes_to_the_ratings_queue() -> None:
    route = celery_app.amqp.router.route({}, TASK_NAME)
    assert route["queue"].name == "ratings"


def test_the_task_limits_stay_under_the_visibility_timeout() -> None:
    assert tasks.decay_reliability.soft_time_limit == 240
    assert tasks.decay_reliability.time_limit == 300


def test_the_beat_entry_runs_the_task_daily_after_the_expiry_tasks() -> None:
    entry = settings.CELERY_BEAT_SCHEDULE["decay-reliability"]
    assert entry["task"] == TASK_NAME
    assert entry["schedule"] == crontab(hour=3, minute=45)
    assert entry["options"] == {"expire_seconds": 3600}


def test_the_task_returns_the_outcome_only() -> None:
    assert tasks.decay_reliability.delay().get() == "no_ratings"


def test_a_worker_failure_raises_fixed_text_only(
    monkeypatch: pytest.MonkeyPatch, ratings_logs: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(services, "decay_reliability", _fail)

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


def test_an_eager_failure_raises_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "decay_reliability", _fail)

    with pytest.raises(MadeUpError, match=SECRET):
        _run(is_eager=True)


def test_the_scheduler_stores_both_new_entries_and_the_admin_lists_them(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The DatabaseScheduler copies CELERY_BEAT_SCHEDULE into its table at
    # each beat start, as `celery beat` does. Its close_old_connections()
    # would close the test's transaction, so it does nothing here.
    monkeypatch.setattr(schedulers, "close_old_connections", lambda: None)
    DatabaseScheduler(app=celery_app)

    decay = PeriodicTask.objects.get(name="decay-reliability")
    assert decay.task == TASK_NAME
    assert decay.expire_seconds == 3600
    tokens = PeriodicTask.objects.get(name="clear-expired-tokens")
    assert tokens.task == "apps.partners.tasks.clear_expired_tokens"
    assert tokens.expire_seconds == 3600

    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    page = client.get(reverse("admin:django_celery_beat_periodictask_changelist"))
    assert page.status_code == 200
    text = page.content.decode()
    assert "decay-reliability" in text
    assert "clear-expired-tokens" in text

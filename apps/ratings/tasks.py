"""Background tasks for ratings (#46, #47, ADR 0027).

Tasks here route to the ``ratings`` queue (``CELERY_TASK_ROUTES``). Each
is safe to run twice: tasks are acknowledged late, so a crash redelivers
them.
"""

import logging

from celery import Task, shared_task
from django.db import transaction
from django.utils import timezone
from sentry_sdk.crons import monitor

from apps.matches.models import Match
from apps.ratings import services
from apps.ratings.models import ReplayReason
from config.sentry import REPLAY_MONITOR_CONFIG, REPLAY_MONITOR_SLUG

logger = logging.getLogger(__name__)


def _update_ratings_for_match(self: "Task[..., str]", match_id: int) -> str:
    """Apply the live rating update for one confirmed Match.

    Takes the Match's row id only and returns only the outcome's value,
    so no personal data passes through the broker or the worker log. Safe
    to run twice: a Match already recorded changes nothing.

    In a worker, any error (the soft time limit included) is logged once with the
    Match id and the error class, and replaced by a fixed-text
    ``LiveUpdateFailed`` with no context, so the worker log never holds
    the original text, which can quote values. Before that, a replay
    request (``replay_needed``, from the Match's date, or today's UTC
    date when the Match is gone) is stored in its own transaction, so the
    next replay or the beat entry rates the Match; if storing it fails
    too, that is logged by class only. In eager mode (tests, local
    development) the original error propagates. The task is not retried.
    """
    try:
        return str(services.update_ratings_for_match(match_id))
    except Exception as exc:
        if self.request.is_eager:
            raise
        error_class = type(exc).__name__
        logger.error(
            "Live update task failed for match %s (%s).", match_id, error_class
        )
        _request_replay_after_failure(match_id)
        raise services.LiveUpdateFailed(match_id, error_class) from None


def _request_replay_after_failure(match_id: int) -> None:
    """Store a replay request for a Match whose live update failed.

    Its own transaction, after the failed one rolled back. Any error is
    logged with the Match id and error class only.
    """
    try:
        with transaction.atomic():
            played_on = (
                Match.objects.filter(pk=match_id)
                .values_list("played_on", flat=True)
                .first()
            )
            services.request_replay(
                timezone.now().date() if played_on is None else played_on,
                ReplayReason.REPLAY_NEEDED,
            )
    except Exception as exc:
        logger.error(
            "Could not request a replay for match %s (%s).",
            match_id,
            type(exc).__name__,
        )


# Named explicitly: queued messages carry the name, so a rename or move
# must not strand them. Both limits stay well under the broker's one-hour
# visibility timeout. Typed Task[..., str]: celery-types gives
# push_request() the task's own parameters, but it takes request fields.
update_ratings_for_match: "Task[..., str]" = shared_task(
    bind=True,
    name="apps.ratings.tasks.update_ratings_for_match",
    soft_time_limit=240,
    time_limit=300,
)(_update_ratings_for_match)


def _replay_ratings(self: "Task[..., str]") -> str:
    """Run the full replay if a replay request is pending (#47).

    Takes no arguments and returns only the outcome's value, so no
    personal data passes through the broker or the worker log. Safe to
    run twice: with no request pending it writes nothing.

    In a worker, any error is logged once with the error class, and
    replaced by a fixed-text ``ReplayFailed`` with no context, so the
    worker log never holds the original text. In eager mode (tests, local
    development) the original error propagates. The task is not retried:
    the requests stay pending, and the next request or the beat entry
    runs it again.

    Every run checks in to the ``replay-ratings`` cron monitor (#65): a
    run that fails or passes the monitor's maximum runtime, or a beat
    entry that stops running, shows as a failed monitor. With Sentry off
    the check-ins go nowhere.
    """
    with monitor(
        monitor_slug=REPLAY_MONITOR_SLUG, monitor_config=REPLAY_MONITOR_CONFIG
    ):
        try:
            return str(services.replay_ratings())
        except Exception as exc:
            if self.request.is_eager:
                raise
            error_class = type(exc).__name__
            logger.error("Replay task failed (%s).", error_class)
            raise services.ReplayFailed(error_class) from None


# Named explicitly, like the live update task. A full replay can take a
# while; both limits stay under the broker's one-hour visibility timeout.
replay_ratings: "Task[..., str]" = shared_task(
    bind=True,
    name="apps.ratings.tasks.replay_ratings",
    soft_time_limit=1500,
    time_limit=1800,
)(_replay_ratings)


def _decay_reliability(self: "Task[..., str]") -> str:
    """Ask for the scheduled replay when it is due (#52, ADR 0060).

    Takes no arguments and returns only the outcome's value, so no
    personal data passes through the broker or the worker log. Safe to
    run twice: a replay that is not due, or a ``scheduled`` request
    already pending, writes nothing. The replay task it queues does the
    rating work.

    In a worker, any error is logged once with the error class, and
    replaced by a fixed-text ``ReplayFailed`` with no context, as in the
    replay task. In eager mode (tests, local development) the original
    error propagates. The task is not retried: the next daily run asks
    again.
    """
    try:
        return str(services.decay_reliability())
    except Exception as exc:
        if self.request.is_eager:
            raise
        error_class = type(exc).__name__
        logger.error("Reliability decay task failed (%s).", error_class)
        raise services.ReplayFailed(error_class) from None


# Named explicitly, like the other rating tasks. It only reads and stores
# one request, so short limits, well under the visibility timeout.
decay_reliability: "Task[..., str]" = shared_task(
    bind=True,
    name="apps.ratings.tasks.decay_reliability",
    soft_time_limit=240,
    time_limit=300,
)(_decay_reliability)

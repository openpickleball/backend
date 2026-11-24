"""Background tasks for ratings (#46, ADR 0027).

Tasks here route to the ``ratings`` queue (``CELERY_TASK_ROUTES``). Each
is safe to run twice: tasks are acknowledged late, so a crash redelivers
them.
"""

import logging

from celery import Task, shared_task

from apps.ratings import services

logger = logging.getLogger(__name__)


def _update_ratings_for_match(self: "Task[..., str]", match_id: int) -> str:
    """Apply the live rating update for one confirmed Match.

    Takes the Match's row id only and returns only the outcome's value,
    so no personal data passes through the broker or the worker log. Safe
    to run twice: a Match already recorded changes nothing.

    In a worker, any error is logged once with the Match id and the error
    class, and replaced by a fixed-text ``LiveUpdateFailed`` with no
    context, so the worker log never holds the original text, which can
    quote values. In eager mode (tests, local development) the original
    error propagates. The task is not retried: the Match waits for the
    next replay (#47).
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
        raise services.LiveUpdateFailed(match_id, error_class) from None


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

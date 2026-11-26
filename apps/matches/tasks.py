"""Background tasks for matches.

Tasks here route to the ``default`` queue (CELERY_TASK_ROUTES has no
pattern for this module). Each is safe to run twice: tasks are
acknowledged late, so a crash redelivers them.
"""

import logging

from celery import Task, shared_task

from apps.matches import notices, results_files, workflow

logger = logging.getLogger(__name__)


# Named explicitly: queued messages carry the name, so a rename or move
# must not strand them. Both limits stay well under the broker's one-hour
# visibility timeout.
@shared_task(
    name="apps.matches.tasks.expire_unconfirmed_matches",
    soft_time_limit=240,
    time_limit=300,
)
def expire_unconfirmed_matches() -> int:
    """Void pending matches past MATCH_CONFIRMATION_EXPIRY_DAYS (#26).

    Beat runs it daily. Takes no arguments and returns only the count, so
    no personal data passes through the broker or the worker log. Safe to
    run twice: a match already voided, or locked by another run, is
    skipped. A match that fails is logged by id and skipped, and the task
    then fails with ``ExpiryFailed`` so the failure is visible.
    """
    return workflow.expire_unconfirmed_matches()


@shared_task(
    name="apps.matches.tasks.expire_disputes_and_holds",
    soft_time_limit=240,
    time_limit=300,
)
def expire_disputes_and_holds() -> int:
    """Void unsettled disputes and possible duplicate holds (ADR 0034, #268).

    Beat runs it daily. A disputed match whose first open dispute is older
    than MATCH_DISPUTE_EXPIRY_DAYS is voided, and so is the newer match of
    a possible duplicate flag open for longer than MATCH_DUPLICATE_HOLD_DAYS
    whose earlier match is confirmed (never one held against a pending or
    disputed earlier match; a correction keeps its original's place in
    that order while its chain keeps the same teams and date). Takes no
    arguments and returns only the count. Safe to run twice: a match
    already voided, or locked by another run, is skipped. A match that
    fails is logged by id and skipped, and the task then fails with
    ``ExpiryFailed`` so the failure is visible.
    """
    return workflow.expire_disputes_and_holds()


def _process_results_file(self: "Task[..., None]", results_file_id: int) -> None:
    """Process one uploaded CSV results file (#71).

    Takes the Results File's row id only and returns nothing, so no
    personal data passes through the broker or the worker log. Safe to run
    twice: a file no longer pending is skipped, and a row this file's batch
    already recorded is reported as recorded again.

    In a worker, any error (the soft time limit included) is logged once
    with the row id and the error class, and replaced by a fixed-text
    ``ResultsFileFailed`` with no context, so the worker log never holds
    the original text, which could quote the file. The file stays pending
    and the sweep queues it again. In eager mode (tests, local
    development) the original error propagates. The task is not retried.
    """
    try:
        results_files.process_results_file(results_file_id)
    except Exception as exc:
        if self.request.is_eager:
            raise
        error_class = type(exc).__name__
        logger.error(
            "Results file task failed for results file %s (%s).",
            results_file_id,
            error_class,
        )
        raise results_files.ResultsFileFailed(results_file_id, error_class) from None


# Named explicitly. A large file can take a while; both limits stay under
# the broker's one-hour visibility timeout, and the hard limit under the
# sweep's REQUEUE_AFTER. Typed like apps.ratings.tasks.
process_results_file: "Task[..., None]" = shared_task(
    bind=True,
    name="apps.matches.tasks.process_results_file",
    soft_time_limit=1500,
    time_limit=1800,
)(_process_results_file)


@shared_task(
    name="apps.matches.tasks.sweep_results_files",
    soft_time_limit=240,
    time_limit=300,
)
def sweep_results_files() -> int:
    """Fail stale pending results files and queue lost ones again (#71).

    Beat runs it every 15 minutes. Takes no arguments and returns only the
    count queued. Safe to run twice: each file is re-checked under its
    own update.
    """
    return results_files.sweep_results_files()


@shared_task(
    name="apps.matches.tasks.send_batch_notices",
    soft_time_limit=600,
    time_limit=720,
)
def send_batch_notices(batch_id: int) -> int:
    """Queue the recorded match notices of one Submission Batch (#208).

    Takes the batch's row id only and returns only the count, so no
    personal data passes through the broker or the worker log. Each
    recipient's failure is logged by row id and error class and skipped.
    Not retried. A redelivery after a crash sends the notices again (at
    least once, like the outbox, ADR 0016).
    """
    return notices.send_batch_notices(batch_id)

"""Background tasks for matches.

Tasks here route to the ``default`` queue (CELERY_TASK_ROUTES has no
pattern for this module). Each is safe to run twice: tasks are
acknowledged late, so a crash redelivers them.
"""

from celery import shared_task

from apps.matches import workflow


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
    disputed earlier match). Takes no arguments and returns only the count.
    Safe to run twice: a match already voided, or locked by another run, is
    skipped. A match that fails is logged by id and skipped, and the task
    then fails with ``ExpiryFailed`` so the failure is visible.
    """
    return workflow.expire_disputes_and_holds()

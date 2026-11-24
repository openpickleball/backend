"""Background tasks for ratings (#46, ADR 0027).

Tasks here route to the ``ratings`` queue (``CELERY_TASK_ROUTES``). Each
is safe to run twice: tasks are acknowledged late, so a crash redelivers
them.
"""

from celery import shared_task

from apps.ratings import services


# Named explicitly: queued messages carry the name, so a rename or move
# must not strand them. Both limits stay well under the broker's one-hour
# visibility timeout.
@shared_task(
    name="apps.ratings.tasks.update_ratings_for_match",
    soft_time_limit=240,
    time_limit=300,
)
def update_ratings_for_match(match_id: int) -> str:
    """Apply the live rating update for one confirmed Match.

    Takes the Match's row id only and returns only the outcome's value,
    so no personal data passes through the broker or the worker log. Safe
    to run twice: a Match already recorded changes nothing.
    """
    return str(services.update_ratings_for_match(match_id))

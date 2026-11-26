"""Data export tasks (issue #39, ADR 0051), on the default queue."""

import logging
from typing import TYPE_CHECKING

from celery import shared_task

from apps.exports import services

if TYPE_CHECKING:
    from celery import Task

logger = logging.getLogger(__name__)


def _build_data_export(self: "Task[..., None]", export_id: int) -> None:
    """Build one requested data export and mail its link.

    Takes the export's row id only and returns nothing, so no personal
    data or key passes through the broker or the worker log. Safe to run
    twice: an export no longer pending, a missing row, and an inactive
    Account are skipped.

    In a worker, any error (the soft time limit included) is logged once
    with the row id and the error class, and replaced by a fixed-text
    ``DataExportFailed`` with no context, so the worker log never holds
    the original text. The export stays pending and the sweep queues it
    again. In eager mode (tests, local development) the original error
    propagates. The task is not retried.
    """
    try:
        services.build_data_export(export_id)
    except Exception as exc:
        if self.request.is_eager:
            raise
        error_class = type(exc).__name__
        logger.error(
            "Data export task failed for data export %s (%s).", export_id, error_class
        )
        raise services.DataExportFailed(export_id, error_class) from None


# Named explicitly. Both limits stay under the broker's one-hour
# visibility timeout and the sweep's REQUEUE_AFTER.
build_data_export: "Task[..., None]" = shared_task(
    bind=True,
    name="apps.exports.tasks.build_data_export",
    soft_time_limit=240,
    time_limit=300,
)(_build_data_export)


@shared_task(
    name="apps.exports.tasks.sweep_data_exports",
    soft_time_limit=240,
    time_limit=300,
)
def sweep_data_exports() -> int:
    """Delete expired data exports and queue lost builds again.

    Beat runs it every 15 minutes. Takes no arguments and returns only the
    count queued. Safe to run twice: each export is re-checked under its
    own update.
    """
    return services.sweep_data_exports()

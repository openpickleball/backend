"""Periodic account tasks (#100), on the default queue."""

from celery import shared_task

from apps.accounts.sessions import sweep_user_sessions


@shared_task(soft_time_limit=240, time_limit=300)
def sweep_sessions() -> int:
    """Delete expired sessions and the user session rows (IP address and
    user agent) of sessions that ended or expired. Safe to run twice.
    Returns how many user session rows it deleted.
    """
    return sweep_user_sessions()

"""Organizer tasks (#229), on the default queue."""

from celery import shared_task

from apps.clubs.invites import sweep_expired
from apps.clubs.models import OrganizerInvite


@shared_task(
    name="apps.clubs.tasks.sweep_organizer_invites",
    soft_time_limit=240,
    time_limit=300,
)
def sweep_organizer_invites() -> int:
    """Delete expired Organizer invites, which hold an address.

    Beat runs it daily. Takes no arguments and returns only the count
    deleted. Safe to run twice.
    """
    return sweep_expired(OrganizerInvite)

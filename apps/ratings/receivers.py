"""Signal receivers that queue rating work (#46, ADR 0027).

``RatingsConfig.ready()`` imports this module, which connects them.
"""

import logging
from typing import Any

from django.db import transaction
from django.dispatch import receiver
from kombu.exceptions import OperationalError

from apps.matches.models import Match
from apps.matches.signals import match_confirmed

logger = logging.getLogger(__name__)

# What publishing a task raises when the broker cannot take it: kombu's
# connection error, or a socket error (ConnectionError is one).
_BROKER_ERRORS = (OperationalError, OSError)


@receiver(match_confirmed, sender=Match, dispatch_uid="ratings.queue_rating_update")
def queue_rating_update(sender: type[Match], match: Match, **kwargs: Any) -> None:
    """Queue the live rating update for ``match`` once the transaction commits.

    Only the row id goes to the broker. Nothing is queued if the
    transaction rolls back. A broker error (an outage) is logged by Match
    id and error class only, and does not fail the confirmation, which has
    already committed: the next replay (#47) rates the Match. Any other
    error propagates, so an eager task's failure in tests is not hidden.
    """
    from apps.ratings.tasks import update_ratings_for_match

    match_id = match.pk

    def enqueue() -> None:
        try:
            update_ratings_for_match.delay(match_id)
        except _BROKER_ERRORS as exc:
            logger.error(
                "Could not queue the rating update for match %s (%s).",
                match_id,
                type(exc).__name__,
            )

    # Not robust: a robust callback would swallow any other error and log
    # its text.
    transaction.on_commit(enqueue)

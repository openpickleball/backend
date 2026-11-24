"""Signal receivers that queue rating work (#46, #47, ADR 0027).

``RatingsConfig.ready()`` imports this module, which connects them.
"""

import logging
from datetime import date
from typing import Any

from django.db import transaction
from django.dispatch import receiver

from apps.matches.models import Match
from apps.matches.signals import MatchChange, match_changed, match_confirmed
from apps.ratings.models import ReplayReason
from apps.ratings.services import _BROKER_ERRORS, request_replay

logger = logging.getLogger(__name__)


@receiver(match_confirmed, sender=Match, dispatch_uid="ratings.queue_rating_update")
def queue_rating_update(sender: type[Match], match: Match, **kwargs: Any) -> None:
    """Queue the live rating update for ``match`` once the transaction commits.

    Only the row id goes to the broker. Nothing is queued if the
    transaction rolls back. A broker error (an outage) is logged by Match
    id and error class only, and does not fail the confirmation, which has
    already committed. Instead a replay request (``replay_needed``, from
    the Match's date) is stored in its own transaction, so the next
    replay, or the beat entry if the broker is still down, rates the
    Match. If storing that request fails too, it is logged by Match id
    and error class only and not raised, so the committed confirmation
    and the commit's other callbacks are unaffected. Any other error from
    queuing propagates, so an eager task's failure in tests is not
    hidden.
    """
    from apps.ratings.tasks import update_ratings_for_match

    match_id = match.pk
    played_on = match.played_on

    def enqueue() -> None:
        try:
            update_ratings_for_match.delay(match_id)
        except _BROKER_ERRORS as exc:
            logger.error(
                "Could not queue the rating update for match %s (%s).",
                match_id,
                type(exc).__name__,
            )
            # After commit: the request needs its own transaction. The
            # confirmation already committed, so no error may escape here.
            try:
                with transaction.atomic():
                    request_replay(played_on, ReplayReason.REPLAY_NEEDED)
            except Exception as request_exc:
                logger.error(
                    "Could not request a replay for match %s (%s).",
                    match_id,
                    type(request_exc).__name__,
                )

    # Not robust: a robust callback would swallow any other error and log
    # its text.
    transaction.on_commit(enqueue)


@receiver(match_changed, sender=Match, dispatch_uid="ratings.request_replay")
def request_rating_replay(
    sender: type[Match],
    match: Match,
    change: MatchChange,
    earliest_date: date,
    **kwargs: Any,
) -> None:
    """Request a replay for a change to what counts (#47).

    Stores one replay request in the transaction that made the change,
    so it rolls back with it, and queues the replay task after commit
    (``request_replay()``). It only inserts that row: the workflow holds
    Match locks here, and the live update takes the engine snapshot row
    before a Match, so locking the snapshot row here could deadlock.
    """
    request_replay(earliest_date, ReplayReason(change))

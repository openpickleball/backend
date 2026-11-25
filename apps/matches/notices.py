"""Dispute and hold notices: tell the Accounts that can act on a match (#272).

``dispute_match`` calls ``schedule_dispute_notices`` when a match becomes
disputed, and ``insert_draft`` (so ``record_match`` and ``edit_match``)
calls ``schedule_hold_notices`` when it writes possible duplicate flags.
Each registers a ``transaction.on_commit`` callback in the change's
transaction, so nothing is queued when the change rolls back. The
callback reads the match again and queues one email per match per
recipient (``render_dispute_notice`` or ``render_hold_notice``, then
``queue_email``); #37 may add a digest.

Recipients come from the rules that decide who may act, never a rule of
their own:

- A dispute: every active Account whose Player is on the other team from
  the disputing player, and, for an Event match, each member of the
  Event's Organizer that ``permissions.settles_dispute`` allows.
- A hold: for the new match and each earlier match it flagged, every
  active Account that ``permissions.vouch_level`` allows (an Event
  match's Organizer members, a self-reported match's players, nobody for
  a club-only match). A voided match, or one no open flag of this
  submission still holds, gets nothing.

A failure while building or queuing notices is logged with the match's
row id and the error class only, and never fails the change, which has
already committed.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.clubs.models import OrganizerMember
from apps.emails.services import queue_email
from apps.events.models import Event
from apps.matches.emails import (
    DISPUTE_NOTICE,
    HOLD_NOTICE,
    render_dispute_notice,
    render_hold_notice,
)
from apps.matches.models import (
    FlagReason,
    Match,
    MatchFlag,
    MatchParticipant,
    MatchStatus,
)
from apps.matches.permissions import settles_dispute, vouch_level

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

logger = logging.getLogger(__name__)


def schedule_dispute_notices(match: Match, disputer_team: str) -> None:
    """Queue the dispute notices for ``match`` once the transaction commits."""
    transaction.on_commit(partial(send_dispute_notices, match.pk, disputer_team))


def schedule_hold_notices(match: Match) -> None:
    """Queue the hold notices for the new ``match`` and the matches its
    possible duplicate flags hold, once the transaction commits."""
    transaction.on_commit(partial(send_hold_notices, match.pk))


def _failed(match_pk: int, exc: Exception) -> int:
    logger.warning(
        "Could not queue notices for match %s (%s).", match_pk, type(exc).__name__
    )
    return 0


def send_dispute_notices(match_pk: int, disputer_team: str) -> int:
    """Queue the dispute notices for match ``match_pk``. Returns how many.

    Runs after the dispute committed, so it reads the match again and
    sends nothing when it is no longer disputed.
    """
    # The broad catch is safe: the dispute has committed, and a notice is
    # never worth failing the caller over. Only the class is logged, since
    # an error's text can quote an address.
    try:
        return _send_dispute_notices(match_pk, disputer_team)
    except Exception as exc:
        return _failed(match_pk, exc)


def send_hold_notices(new_match_pk: int) -> int:
    """Queue the hold notices for the match ``new_match_pk`` flagged as a
    possible duplicate and the earlier matches its flags hold. Returns how
    many."""
    # The broad catch is safe for the same reasons as in
    # send_dispute_notices.
    try:
        return _send_hold_notices(new_match_pk)
    except Exception as exc:
        return _failed(new_match_pk, exc)


def _organizer_members(match: Match) -> list["_User"]:
    """The active member Accounts of the match's Event's Organizer, by id."""
    if match.event_id is None:
        return []
    organizer_id = (
        Event.objects.filter(pk=match.event_id)
        .values_list("organizer_id", flat=True)
        .get()
    )
    return list(
        get_user_model()
        .objects.filter(
            is_active=True,
            pk__in=OrganizerMember.objects.filter(organizer_id=organizer_id).values(
                "account_id"
            ),
        )
        .order_by("pk")
    )


def _participants(match: Match) -> list[MatchParticipant]:
    return list(
        MatchParticipant.objects.filter(match_id=match.pk)
        .select_related("player__account")
        .order_by("pk")
    )


def _send_dispute_notices(match_pk: int, disputer_team: str) -> int:
    # workflow imports this module, so its helpers are imported here.
    from apps.matches.workflow import dispute_started_at

    match = Match.objects.filter(pk=match_pk).first()
    if match is None or match.status != MatchStatus.DISPUTED:
        return 0
    started = dispute_started_at(match)
    if started is None:
        return 0
    ends_on = _ends_on(started, settings.MATCH_DISPUTE_EXPIRY_DAYS)
    sent: set[int] = set()
    for participant in _participants(match):
        account = participant.player.account
        if participant.team == disputer_team or not account.is_active:
            continue
        if account.pk in sent:
            continue
        message = render_dispute_notice(
            account, match, team=participant.team, ends_on=ends_on
        )
        queue_email(message, template=DISPUTE_NOTICE)
        sent.add(account.pk)
    for member in _organizer_members(match):
        if member.pk in sent or not settles_dispute(member, match):
            continue
        message = render_dispute_notice(member, match, team=None, ends_on=ends_on)
        queue_email(message, template=DISPUTE_NOTICE)
        sent.add(member.pk)
    return len(sent)


def _send_hold_notices(new_match_pk: int) -> int:
    from apps.matches.workflow import _open_holds_of

    flags = MatchFlag.objects.filter(
        match_id=new_match_pk,
        reason=FlagReason.POSSIBLE_DUPLICATE,
        resolved_at__isnull=True,
    )
    flag_pks = list(flags.values_list("pk", flat=True))
    if not flag_pks:
        return 0
    targets = [new_match_pk]
    for earlier in flags.order_by("pk").values_list("duplicate_of_id", flat=True):
        if earlier is not None and earlier not in targets:
            targets.append(earlier)
    count = 0
    for target_pk in targets:
        match = Match.objects.filter(pk=target_pk).first()
        if match is None or match.status == MatchStatus.VOIDED:
            continue
        holding = list(
            _open_holds_of(match)
            .filter(pk__in=flag_pks)
            .values_list("created_at", flat=True)
        )
        if not holding:
            continue
        ends_on = _ends_on(max(holding), settings.MATCH_DUPLICATE_HOLD_DAYS)
        count += _send_holds_for(match, ends_on)
    return count


def _send_holds_for(match: Match, ends_on: date) -> int:
    """One hold notice to each Account that may vouch for ``match``."""
    teams: dict[int, str] = {}
    candidates: list[_User] = []
    for participant in _participants(match):
        account = participant.player.account
        teams.setdefault(account.pk, participant.team)
        candidates.append(account)
    candidates += _organizer_members(match)
    sent: set[int] = set()
    for account in candidates:
        if account.pk in sent or not account.is_active:
            continue
        if vouch_level(account, match) is None:
            continue
        message = render_hold_notice(
            account, match, team=teams.get(account.pk), ends_on=ends_on
        )
        queue_email(message, template=HOLD_NOTICE)
        sent.add(account.pk)
    return len(sent)


def _ends_on(start: datetime, days: int) -> date:
    """The UTC date ``days`` days after ``start``."""
    return (start.astimezone(UTC) + timedelta(days=days)).date()

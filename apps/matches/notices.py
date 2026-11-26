"""Dispute, hold, and recorded match notices: tell the Accounts that can
act on a match (#272, #208).

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
  submission still holds, gets nothing. The skip for an earlier match is
  per recipient: a recipient is not told again while their own side (the
  players of their team, or the Organizer) still has an older open hold
  on that match to vouch on (older means a lower flag id than this
  submission's flags; its other match not voided). They were told when
  that hold began, and the vouch they owe covers the new flag too. A side
  that has vouched on every older hold is told, since the new flag needs
  its new vouch. The new match's recipients are always notified.

Recorded match notices (#208, ADR 0041) tell the players of a match a
trusted submitter recorded, which counts without their confirmation, so
they can dispute a wrong one:

- ``insert_draft`` (so ``record_match`` and ``edit_match``) calls
  ``schedule_recorded_notice`` when it writes a confirmed match with an
  Event and no Submission Batch. The callback reads the match again and
  sends nothing once it is neither confirmed nor disputed.
- ``record_match``'s duplicate path calls it too when the Organizer's
  exact duplicate confirms a pending self-submission, since the other
  team never confirmed it.
- A batch path calls ``schedule_batch_notices`` once, when the batch is
  done, so a player gets one email for the whole batch, listing each of
  their matches in it that is confirmed or disputed when the task runs.
  For a CSV upload that is when its Results File stops processing, or,
  when a bulk void stopped it, after that bulk void, for the matches it
  left counting. The ``send_batch_notices`` task in
  ``apps.matches.tasks`` sends them.

Recipients are the players ``permissions.participant_player`` allows to
dispute the match: each participant whose Account is active.

A failure while building or queuing notices is logged with the match's
(or the batch's) row id and the error class only (Celery's soft time
limit is raised again, so a task out of time stops), and never fails the change, which has
already committed. It is caught per recipient and per held match, so one
failed notice never drops the others.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from kombu.exceptions import OperationalError

from apps.clubs.models import OrganizerMember
from apps.emails.services import queue_email
from apps.events.models import Event
from apps.matches.emails import (
    DISPUTE_NOTICE,
    HOLD_NOTICE,
    RECORDED_NOTICE,
    render_dispute_notice,
    render_hold_notice,
    render_recorded_notice,
)
from apps.matches.models import (
    FlagReason,
    Match,
    MatchFlag,
    MatchParticipant,
    MatchStatus,
    MatchVouch,
)
from apps.matches.permissions import (
    participant_player,
    settles_dispute,
    vouch_level,
)
from apps.players.models import Player

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


def schedule_recorded_notice(match: Match) -> None:
    """Queue the recorded match notices for ``match`` once the transaction
    commits (#208)."""
    transaction.on_commit(partial(send_recorded_notice, match.pk))


def schedule_batch_notices(batch_id: int) -> None:
    """Queue the ``send_batch_notices`` task for the Submission Batch
    ``batch_id`` once the transaction commits (#208).

    The task sends one recorded match notice per player. It runs on its
    own, with its own time limits, so a large batch never shares the time
    of the task that finished it. Outside a transaction it is queued at
    once.
    """
    transaction.on_commit(partial(_enqueue_batch_notices, batch_id))


def _enqueue_batch_notices(batch_id: int) -> None:
    """Queue the task. A broker error is logged by id and class only, and
    the batch's notices are lost (ADR 0041)."""
    from apps.matches.tasks import send_batch_notices as task

    try:
        task.delay(batch_id)
    except (OperationalError, OSError) as exc:
        logger.error(
            "Could not queue notices for submission batch %s (%s).",
            batch_id,
            type(exc).__name__,
        )


def _failed(match_pk: int, exc: Exception, *, what: str = "match") -> int:
    logger.warning(
        "Could not queue notices for %s %s (%s).", what, match_pk, type(exc).__name__
    )
    return 0


def _queue_one(
    match_pk: int,
    template: str,
    build: Callable[[], EmailMultiAlternatives | None],
    *,
    what: str = "match",
) -> bool:
    """Build and queue one recipient's notice; True when one was queued.

    ``build`` returns None when the recipient gets no notice. A failure is
    logged by ``_failed`` and does not stop the other recipients.
    """
    # The broad catch is safe: the change has committed, and one
    # recipient's notice is never worth dropping the others' over. Only
    # the class is logged, since an error's text can quote an address.
    try:
        message = build()
        if message is None:
            return False
        queue_email(message, template=template)
    except SoftTimeLimitExceeded:
        # The task's time is up: stop, rather than log every recipient
        # left as a failure until the hard limit.
        raise
    except Exception as exc:
        _failed(match_pk, exc, what=what)
        return False
    return True


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
    except SoftTimeLimitExceeded:
        raise
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
    except SoftTimeLimitExceeded:
        raise
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
    seen: set[int] = set()
    count = 0
    for participant in _participants(match):
        account = participant.player.account
        if participant.team == disputer_team or not account.is_active:
            continue
        if account.pk in seen:
            continue
        seen.add(account.pk)
        count += _queue_one(
            match.pk,
            DISPUTE_NOTICE,
            partial(
                render_dispute_notice,
                account,
                match,
                team=participant.team,
                ends_on=ends_on,
            ),
        )
    for member in _organizer_members(match):
        if member.pk in seen:
            continue
        seen.add(member.pk)
        count += _queue_one(
            match.pk,
            DISPUTE_NOTICE,
            partial(_organizer_dispute_notice, member, match, ends_on),
        )
    return count


def _organizer_dispute_notice(
    member: "_User", match: Match, ends_on: date
) -> EmailMultiAlternatives | None:
    if not settles_dispute(member, match):
        return None
    return render_dispute_notice(member, match, team=None, ends_on=ends_on)


def _send_hold_notices(new_match_pk: int) -> int:
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
        # The broad catch is safe for the same reasons as in _queue_one:
        # one held match's failure never drops the other held matches.
        try:
            count += _send_hold_target(target_pk, new_match_pk, flag_pks)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            _failed(target_pk, exc)
    return count


def _send_hold_target(target_pk: int, new_match_pk: int, flag_pks: list[int]) -> int:
    """The hold notices for one match a flag of this submission holds."""
    from apps.matches.workflow import _open_holds_of

    match = Match.objects.filter(pk=target_pk).first()
    if match is None or match.status == MatchStatus.VOIDED:
        return 0
    holds = _open_holds_of(match)
    holding = list(holds.filter(pk__in=flag_pks).values_list("created_at", flat=True))
    if not holding:
        return 0
    # Only flags older than this submission's (flag ids are insertion
    # order) can make an earlier match's recipient owe a vouch already: a
    # newer flag's callback skips this match because of ours. The new
    # match has no older flag of its own to wait on.
    older = (
        [] if target_pk == new_match_pk else list(holds.filter(pk__lt=min(flag_pks)))
    )
    ends_on = _ends_on(max(holding), settings.MATCH_DUPLICATE_HOLD_DAYS)
    return _send_holds_for(match, ends_on, older)


def _send_holds_for(match: Match, ends_on: date, older: list[MatchFlag]) -> int:
    """One hold notice to each Account that may vouch for ``match`` and
    whose side owes no vouch on an ``older`` flag."""
    teams: dict[int, str] = {}
    candidates: list[_User] = []
    for participant in _participants(match):
        account = participant.player.account
        teams.setdefault(account.pk, participant.team)
        candidates.append(account)
    candidates += _organizer_members(match)
    seen: set[int] = set()
    count = 0
    for account in candidates:
        if account.pk in seen or not account.is_active:
            continue
        seen.add(account.pk)
        count += _queue_one(
            match.pk,
            HOLD_NOTICE,
            partial(
                _hold_notice, account, match, teams.get(account.pk), ends_on, older
            ),
        )
    return count


def _hold_notice(
    account: "_User",
    match: Match,
    team: str | None,
    ends_on: date,
    older: list[MatchFlag],
) -> EmailMultiAlternatives | None:
    if vouch_level(account, match) is None:
        return None
    if any(not _side_vouched(flag, match, team) for flag in older):
        # The recipient's side still owes a vouch on an older open hold of
        # this match, so it was told when that hold began, and the vouch
        # it owes covers this flag too: vouch_for_match covers every open
        # flag that holds the match. Telling it again for every new near
        # duplicate would grow with the square of the submissions. A side
        # that has vouched on every older hold must be told, since this
        # flag needs its new vouch.
        return None
    return render_hold_notice(account, match, team=team, ends_on=ends_on)


def _side_vouched(flag: MatchFlag, match: Match, team: str | None) -> bool:
    """True when the recipient's side has vouched for ``match`` on ``flag``.

    For an Event match the side is its Organizer: any vouch row on the
    flag for the match (only the Organizer's actors write one). For a
    self-reported match it is the recipient's team: a vouch from an
    Account whose Player plays on that team in the match.
    """
    vouches = MatchVouch.objects.filter(flag=flag, match_id=match.pk)
    if match.event_id is not None:
        return vouches.exists()
    if team is None:
        return False
    teammates = MatchParticipant.objects.filter(match_id=match.pk, team=team).values(
        "player__account_id"
    )
    return vouches.filter(account_id__in=teammates).exists()


_RECORDED_STATUSES = (MatchStatus.CONFIRMED, MatchStatus.DISPUTED)


def send_recorded_notice(match_pk: int) -> int:
    """Queue the recorded match notices for match ``match_pk``. Returns how
    many.

    Runs after the match committed, so it reads the match again and sends
    nothing when it is gone, or no longer confirmed or disputed.
    """
    # The broad catch is safe for the same reasons as in
    # send_dispute_notices.
    try:
        matches = list(Match.objects.filter(pk=match_pk, status__in=_RECORDED_STATUSES))
        return _send_recorded(matches, match_pk, what="match")
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        return _failed(match_pk, exc)


def send_batch_notices(batch_id: int) -> int:
    """Queue one recorded match notice per player for the Submission Batch
    ``batch_id``, listing each of their matches in it that is confirmed or
    disputed, in the batch's order. Returns how many."""
    # The broad catch is safe for the same reasons as in
    # send_dispute_notices.
    try:
        matches = list(
            Match.objects.filter(
                submission_batch_id=batch_id, status__in=_RECORDED_STATUSES
            ).order_by("sequence", "pk")
        )
        return _send_recorded(matches, batch_id, what="submission batch")
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        return _failed(batch_id, exc, what="submission batch")


def _send_recorded(matches: list[Match], log_pk: int, *, what: str) -> int:
    """One notice per participating Player listing their ``matches``."""
    order = {match.pk: index for index, match in enumerate(matches)}
    by_pk = {match.pk: match for match in matches}
    players: dict[int, tuple[Player, list[Match]]] = {}
    participants = MatchParticipant.objects.filter(
        match_id__in=list(by_pk)
    ).select_related("player__account")
    for participant in sorted(participants, key=lambda p: (order[p.match_id], p.pk)):
        player = participant.player
        _player, listed = players.setdefault(player.pk, (player, []))
        listed.append(by_pk[participant.match_id])
    count = 0
    for player, listed in players.values():
        count += _queue_one(
            log_pk,
            RECORDED_NOTICE,
            partial(_recorded_notice, player, listed),
            what=what,
        )
    return count


def _recorded_notice(
    player: Player, matches: list[Match]
) -> EmailMultiAlternatives | None:
    """The player's notice, listing only the matches they may dispute."""
    if not player.account.is_active:
        return None
    mine = [match for match in matches if participant_player(player.account, match)]
    if not mine:
        return None
    return render_recorded_notice(player, mine)


def _ends_on(start: datetime, days: int) -> date:
    """The UTC date ``days`` days after ``start``."""
    return (start.astimezone(UTC) + timedelta(days=days)).date()

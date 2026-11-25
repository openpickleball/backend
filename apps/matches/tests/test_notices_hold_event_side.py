"""The Organizer side of the per-recipient hold notice skip (#272).

For an Event match the side that vouches is its Event's verified
Organizer. A member is not told again about an earlier Event match while
the Organizer still owes a vouch on an older open hold of it, and is told
once the Organizer has vouched on every older hold. Made-up data only.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.core import mail

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.emails.models import QueuedEmail
from apps.events.models import Event
from apps.matches.emails import HOLD_NOTICE
from apps.matches.models import Match, MatchFlag, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import vouch_for_match
from apps.players.models import Player

pytestmark = pytest.mark.django_db

Capture = Callable[..., Any]


def _submit(
    players: list[Player], games: tuple[tuple[int, int], ...], **links: Any
) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=links.pop("submitter", players[0].account),
        **links,
    ).match


def _told(match: Match, account: User) -> int:
    """Hold notices about ``match`` to ``account``, sent or still queued
    (sent rows are deleted, so the two never overlap)."""
    code = str(match.public_code)
    sent = sum(m.to == [account.email] and code in str(m.body) for m in mail.outbox)
    queued = sum(
        code in row.body
        for row in QueuedEmail.objects.filter(template=HOLD_NOTICE, to=account.email)
    )
    return sent + queued


def _held_event_match(
    players: list[Player], event: Event, capture: Capture
) -> tuple[User, Match, Match]:
    """A confirmed Event match, a free member, and a self-reported near
    duplicate whose older open flag holds the Event match."""
    free: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=free, role=MemberRole.MATCH_MANAGER
    )
    earlier = _submit(players, ((11, 7), (11, 9)), event=event, submitter=free)
    assert earlier.status == MatchStatus.CONFIRMED
    with capture(execute=True):
        other = _submit(players, ((11, 2), (11, 3)))
    assert _told(earlier, free) == 1
    return free, earlier, other


def test_a_member_is_not_told_again_while_the_organizer_owes_a_vouch(
    players: list[Player], event: Event, django_capture_on_commit_callbacks: Capture
) -> None:
    free, earlier, _other = _held_event_match(
        players, event, django_capture_on_commit_callbacks
    )

    with django_capture_on_commit_callbacks(execute=True):
        _submit(players, ((11, 4), (11, 5)))

    assert _told(earlier, free) == 1


def test_a_member_is_told_again_once_the_organizer_vouched_on_the_older_hold(
    players: list[Player], event: Event, django_capture_on_commit_callbacks: Capture
) -> None:
    free, earlier, other = _held_event_match(
        players, event, django_capture_on_commit_callbacks
    )
    vouch_for_match(earlier, actor=free)
    assert MatchFlag.objects.get(match=other, duplicate_of=earlier).resolved_at is None

    with django_capture_on_commit_callbacks(execute=True):
        _submit(players, ((11, 4), (11, 5)))

    assert _told(earlier, free) == 2

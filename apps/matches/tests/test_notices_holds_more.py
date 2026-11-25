"""More hold notice cases (#272): corrections, several earlier matches,
flags resolved before the callback, members who play, and rollbacks.
Made-up data only.
"""

from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from django.core import mail
from django.db import transaction

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches import notices
from apps.matches.models import FlagResolution, Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import edit_match, resolve_flags
from apps.players.models import Player

pytestmark = pytest.mark.django_db

REASON = "Made-up reason for a test."
Capture = Callable[..., Any]


def _submit(
    players: list[Player],
    games: tuple[tuple[int, int], ...],
    played_on: date = WORKFLOW_PLAYED_ON,
    **links: Any,
) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=played_on,
        submitted_by_user=links.pop("submitter", players[0].account),
        **links,
    ).match


def _per_account_and_code(matches: list[Match]) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for message in mail.outbox:
        for match in matches:
            if str(match.public_code) in str(message.body):
                counts[(message.to[0], str(match.public_code))] += 1
    return counts


def _every(players: list[Player], matches: list[Match]) -> Counter[tuple[str, str]]:
    return Counter(
        {
            (player.account.email, str(match.public_code)): 1
            for player in players
            for match in matches
        }
    )


def test_a_correction_that_lands_on_another_match_notifies_both(
    players: list[Player],
    confirmed: Match,
    moderator: User,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    elsewhere = _submit(players, ((11, 5), (11, 6)), played_on=date(2026, 5, 1))

    with django_capture_on_commit_callbacks(execute=True):
        correction = edit_match(
            elsewhere,
            actor=moderator,
            reason=REASON,
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=((11, 2), (11, 3)),
            played_on=WORKFLOW_PLAYED_ON,
        )

    assert correction.played_on == WORKFLOW_PLAYED_ON
    assert _per_account_and_code([correction, confirmed, elsewhere]) == _every(
        players, [correction, confirmed]
    )


def test_a_near_duplicate_of_two_unheld_matches_notifies_all_three(
    players: list[Player],
    confirmed: Match,
    moderator: User,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    second = _submit(players, ((11, 5), (11, 6)))
    resolve_flags(
        second, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
    )

    with django_capture_on_commit_callbacks(execute=True):
        third = _submit(players, ((11, 2), (11, 3)))

    assert _per_account_and_code([confirmed, second, third]) == _every(
        players, [confirmed, second, third]
    )
    assert len(mail.outbox) == 12


def test_a_flag_resolved_before_the_callback_sends_nothing(
    players: list[Player],
    confirmed: Match,
    moderator: User,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        newer = _submit(players, ((11, 2), (11, 3)))
    resolve_flags(
        newer, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
    )

    assert any(
        getattr(callback, "func", None) is notices.send_hold_notices
        for callback in callbacks
    )
    for callback in callbacks:
        callback()

    assert mail.outbox == []


def test_a_member_who_plays_gets_no_hold_notice_for_the_event_match(
    players: list[Player], event: Event, django_capture_on_commit_callbacks: Capture
) -> None:
    free: User = UserFactory.create()
    for account in (free, players[2].account):
        OrganizerMemberFactory.create(
            organizer=event.organizer, account=account, role=MemberRole.MATCH_MANAGER
        )
    earlier = _submit(players, ((11, 7), (11, 9)), event=event, submitter=free)
    assert earlier.status == MatchStatus.CONFIRMED

    with django_capture_on_commit_callbacks(execute=True):
        newer = _submit(players, ((11, 2), (11, 3)))

    counts = _per_account_and_code([earlier, newer])
    assert counts[(players[2].account.email, str(earlier.public_code))] == 0
    assert counts[(players[2].account.email, str(newer.public_code))] == 1
    assert counts[(free.email, str(earlier.public_code))] == 1


def test_a_rolled_back_near_duplicate_sends_nothing(
    players: list[Player], confirmed: Match, django_capture_on_commit_callbacks: Capture
) -> None:
    class Rollback(Exception):
        pass

    def submit_then_fail() -> None:
        with transaction.atomic():
            _submit(players, ((11, 2), (11, 3)))
            raise Rollback

    with (
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(Rollback),
    ):
        submit_then_fail()

    assert mail.outbox == []
    assert Match.objects.count() == 1

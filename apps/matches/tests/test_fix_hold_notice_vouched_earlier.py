"""A match vouched for on its old hold is told about a new one (#272, F8).

An older open flag counts as already announced for an earlier match only
while that match has not been vouched for on it. Once its players have
vouched, the old flag stays open only because the other match is not
vouched for, and a new flag against it needs a new vouch, so its players
must be told. Made-up data only.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.core import mail

from apps.emails.models import QueuedEmail
from apps.matches.emails import HOLD_NOTICE
from apps.matches.models import FlagReason, Match, MatchFlag, MatchFormat
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import vouch_for_match
from apps.players.models import Player

pytestmark = pytest.mark.django_db


def _submit(players: list[Player], games: tuple[tuple[int, int], ...]) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def _count(match: Match) -> int:
    return sum(str(match.public_code) in str(m.body) for m in mail.outbox)


def _queued(match: Match) -> int:
    """Hold notice rows for ``match`` queued and not sent (sent rows are
    deleted), so ``_count`` plus this covers every notice."""
    return sum(
        str(match.public_code) in row.body
        for row in QueuedEmail.objects.filter(template=HOLD_NOTICE)
    )


def test_a_match_vouched_for_on_its_old_hold_is_told_about_a_new_one(
    players: list[Player],
    confirmed: Match,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    other = _submit(players, ((11, 2), (11, 3)))
    vouch_for_match(confirmed, actor=players[0].account)
    vouch_for_match(confirmed, actor=players[2].account)
    old_flag = MatchFlag.objects.get(match=other, duplicate_of=confirmed)
    assert old_flag.reason == FlagReason.POSSIBLE_DUPLICATE
    assert old_flag.resolved_at is None

    with django_capture_on_commit_callbacks(execute=True):
        newest = _submit(players, ((11, 4), (11, 5)))

    assert _count(confirmed) + _queued(confirmed) == 4
    assert _count(newest) + _queued(newest) == 4
    # The other match was never vouched for on its old hold: already told.
    assert _count(other) + _queued(other) == 0
    assert (
        len({m.to[0] for m in mail.outbox if str(confirmed.public_code) in str(m.body)})
        == 4
    )

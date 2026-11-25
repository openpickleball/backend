"""A newer flag does not hide an earlier match's first hold notice (#272, F7).

The skip for an earlier match already on hold counts only flags older
than this submission's. Otherwise, when a second near duplicate commits
before the first one's callback runs, each callback sees the other's
flag and the earlier match's players are never told. Made-up data only.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.core import mail

from apps.matches.models import Match, MatchFormat
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
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


def test_earlier_match_is_told_once_when_callbacks_run_out_of_order(
    players: list[Player],
    confirmed: Match,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=False) as second_callbacks:
        second = _submit(players, ((11, 2), (11, 3)))

    with django_capture_on_commit_callbacks(execute=True):
        third = _submit(players, ((11, 4), (11, 5)))
    assert _count(confirmed) == 0
    assert _count(third) == 4

    for callback in second_callbacks:
        callback()

    assert _count(confirmed) == 4
    assert _count(second) == 4
    assert (
        len({m.to[0] for m in mail.outbox if str(confirmed.public_code) in str(m.body)})
        == 4
    )

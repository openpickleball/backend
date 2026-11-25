"""Whether a held match's recipient is told again is decided per side
(#272, F9).

A recipient of an earlier match is not told about a new hold while their
own side still owes a vouch on an older open hold of that match: their
pending vouch covers every open flag. A side that has vouched on every
older hold must be told, since the new flag needs their new vouch.
Made-up data only.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.core import mail

from apps.emails.models import QueuedEmail
from apps.matches.emails import HOLD_NOTICE
from apps.matches.models import Match, MatchFlag, MatchFormat
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


def _told(match: Match) -> list[str]:
    """Every recipient of a hold notice for ``match``, sent or still queued
    (sent rows are deleted, so the two never overlap)."""
    code = str(match.public_code)
    sent = [m.to[0] for m in mail.outbox if code in str(m.body)]
    queued = [
        row.to
        for row in QueuedEmail.objects.filter(template=HOLD_NOTICE)
        if code in row.body
    ]
    return sorted(sent + queued)


def test_only_the_side_that_vouched_is_told_about_a_new_hold(
    players: list[Player],
    confirmed: Match,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    other = _submit(players, ((11, 2), (11, 3)))
    # Team A vouches for the earlier match on the old hold; team B has not.
    vouch_for_match(confirmed, actor=players[0].account)
    assert (
        MatchFlag.objects.get(match=other, duplicate_of=confirmed).resolved_at is None
    )

    with django_capture_on_commit_callbacks(execute=True):
        newest = _submit(players, ((11, 4), (11, 5)))

    assert _told(confirmed) == sorted(p.account.email for p in players[:2])
    assert _told(newest) == sorted(p.account.email for p in players)
    # Nobody vouched for the other match on the old hold: nobody is told again.
    assert _told(other) == []

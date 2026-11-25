"""A repeat near duplicate does not mail the matches already on hold (#272, F3).

Each new near duplicate of a lineup and date flags every earlier match,
so mailing each earlier match again would send a number of emails that
grows with the square of the submissions. An earlier match that an older
open flag already held was announced when its hold began, so only the new
match's recipients are told. Made-up data only.
"""

from collections import Counter
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


def _per_code(matches: list[Match]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for message in mail.outbox:
        for match in matches:
            if str(match.public_code) in str(message.body):
                counts[str(match.public_code)] += 1
    return counts


def test_a_third_near_duplicate_mails_only_its_own_recipients(
    players: list[Player], django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        a = _submit(players, ((11, 7), (11, 9)))
        b = _submit(players, ((11, 2), (11, 3)))
    assert _per_code([a, b]) == {
        str(a.public_code): 4,
        str(b.public_code): 4,
    }
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        c = _submit(players, ((11, 4), (11, 5)))

    assert _per_code([a, b, c]) == {str(c.public_code): 4}
    assert len(mail.outbox) == 4

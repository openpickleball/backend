"""The notices say when a dispute or hold may end, honestly (#272, F1).

The expiry task runs once a day at 03:30 UTC and voids a match once its
start is older than the limit, so the void lands on the end date or the
next day. The notices say so, in UTC. Made-up data only.
"""

from datetime import date

import pytest
from django.core.mail import EmailMultiAlternatives

from apps.matches.emails import render_dispute_notice, render_hold_notice
from apps.matches.models import MatchFormat, MatchStatus
from apps.matches.tests.factories import MatchFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

ENDS_ON = date(2026, 7, 1)
DISPUTE = (
    "If nobody settles the dispute first, the match is voided on July 1, 2026 "
    "or the next day (UTC), and does not count."
)
HOLD = (
    "If nobody settles the hold first, the newer of the two matches is voided "
    "on July 1, 2026 or the next day (UTC), as long as the earlier one is "
    "confirmed by then."
)


def _texts(message: EmailMultiAlternatives) -> list[str]:
    [(html, _mimetype)] = message.alternatives
    return [str(message.body), str(html)]


def test_dispute_notice_says_on_the_date_or_the_next_day_utc() -> None:
    match = MatchFactory.create(format=MatchFormat.SINGLES, status=MatchStatus.DISPUTED)
    account = PlayerFactory.create().account

    message = render_dispute_notice(account, match, team=None, ends_on=ENDS_ON)

    for text in _texts(message):
        assert DISPUTE in text
        assert "is not settled by" not in text


def test_hold_notice_says_on_the_date_or_the_next_day_utc() -> None:
    match = MatchFactory.create(format=MatchFormat.SINGLES, status=MatchStatus.PENDING)
    account = PlayerFactory.create().account

    message = render_hold_notice(account, match, team=None, ends_on=ENDS_ON)

    for text in _texts(message):
        assert HOLD in text
        assert "If the hold is still open on" not in text

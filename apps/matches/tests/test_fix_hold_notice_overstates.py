"""The hold notice does not overstate how a hold ends (#272, F4).

A hold ends when each match is vouched for, and also on its own, and the
earlier match does not always count afterward. Made-up data only.
"""

from datetime import date

import pytest

from apps.matches.emails import render_hold_notice
from apps.matches.models import MatchFormat, MatchStatus, Team
from apps.matches.tests.factories import MatchFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("team", [Team.A, None])
def test_hold_notice_says_the_hold_can_end_and_claims_no_outcome(
    team: Team | None,
) -> None:
    match = MatchFactory.create(format=MatchFormat.SINGLES, status=MatchStatus.PENDING)
    account = PlayerFactory.create().account

    message = render_hold_notice(account, match, team=team, ends_on=date(2026, 7, 1))

    [(html, _mimetype)] = message.alternatives
    for text in (str(message.body), str(html)):
        assert (
            "Both are held from counting until each is vouched for, or the hold ends."
            in text
        )
        assert "the earlier one counts" not in text

"""A correction of a Match that was never rated live is rated live (#46).

A pending self-reported Match is corrected before anyone confirms it. Its
correction is pending too, and once both teams confirm it, the live
update rates it: no Match in its ``corrects`` chain changed a rating.
Made-up data only.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.workflow import confirm_match, edit_match
from apps.players.models import GenderDivision
from apps.ratings.models import RatingChangeKind, RatingHistory, RatingState
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


def test_a_correction_of_an_unrated_match_is_rated_live(
    django_capture_on_commit_callbacks: Capture,
) -> None:
    original, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        status=MatchStatus.PENDING,
        played_on=date(2026, 3, 1),
    )
    a, b = players[Team.A][0], players[Team.B][0]

    with django_capture_on_commit_callbacks(execute=True):
        correction = edit_match(
            original,
            actor=_moderator(),
            reason="Made-up reason for a test.",
            format=MatchFormat.SINGLES,
            team_a=[a],
            team_b=[b],
            games=((11, 4), (11, 6)),
            played_on=date(2026, 3, 2),
        )
        assert correction.status == MatchStatus.PENDING
        confirm_match(correction, actor=a.account)
        confirm_match(Match.objects.get(pk=correction.pk), actor=b.account)

    assert correction.corrects_id == original.pk
    for player in (a, b):
        [history] = RatingHistory.objects.filter(player=player)
        assert history.match_id == correction.pk
        assert history.kind == RatingChangeKind.LIVE_UPDATE
        assert RatingState.objects.get(player=player).matches_played == 1

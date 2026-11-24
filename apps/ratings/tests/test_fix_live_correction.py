"""A correction of a live-rated Match goes to a replay, not a live update (#46).

A confirmed self-reported Match is rated live. A player disputes it, and a
moderator's edit creates a confirmed correction, which sends
``match_confirmed``. The old Match's effect is still in the ratings and
the snapshot, so recording the correction on top would count the match
twice. The live update writes nothing and hands every player in the
correction chain to ``schedule_replay``, from the chain's earliest date
(``replay_needed``). Made-up data only.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.workflow import confirm_match, dispute_match, edit_match
from apps.players.models import GenderDivision, Player
from apps.ratings import services
from apps.ratings.models import RatingHistory, RatingState
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}
FIRST_DAY = date(2026, 3, 1)
CORRECTED_DAY = date(2026, 3, 3)


def _moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


def test_a_correction_of_a_live_rated_match_needs_a_replay(
    django_capture_on_commit_callbacks: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], date]] = []
    monkeypatch.setattr(
        services,
        "schedule_replay",
        lambda found, earliest_date: calls.append(
            (sorted(str(p.public_code) for p in found), earliest_date)
        ),
    )
    original, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        status=MatchStatus.PENDING,
        played_on=FIRST_DAY,
    )
    a: Player = players[Team.A][0]
    b: Player = players[Team.B][0]
    with django_capture_on_commit_callbacks(execute=True):
        confirm_match(original, actor=a.account)
        confirm_match(Match.objects.get(pk=original.pk), actor=b.account)
    assert RatingHistory.objects.filter(match=original).count() == 2

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(Match.objects.get(pk=original.pk), actor=b.account)
        correction = edit_match(
            Match.objects.get(pk=original.pk),
            actor=_moderator(),
            reason="Made-up reason for a test.",
            format=MatchFormat.SINGLES,
            team_a=[a],
            team_b=[b],
            games=((11, 4), (11, 6)),
            played_on=CORRECTED_DAY,
        )

    assert correction.status == MatchStatus.CONFIRMED
    assert correction.corrects_id == original.pk
    assert not RatingHistory.objects.filter(match=correction).exists()
    assert {s.matches_played for s in RatingState.objects.all()} == {1}
    assert calls == [(sorted([str(a.public_code), str(b.public_code)]), FIRST_DAY)]
    assert services.update_ratings_for_match(correction.pk) == "replay_needed"

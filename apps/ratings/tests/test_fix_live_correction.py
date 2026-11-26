"""A correction of a live-rated Match goes to a replay, not a live update (#46).

A confirmed Event Match is rated live. A player disputes it, and the
Event's verified Organizer settles the dispute with an edit (ADR 0034),
which creates a confirmed correction and sends ``match_confirmed``. The old Match's effect is still in the ratings and
the snapshot, so recording the correction on top would count the match
twice. The live update writes nothing and hands every player in the
correction chain to ``schedule_replay``, from the chain's earliest date
(``replay_needed``). Made-up data only.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.workflow import confirm_match, dispute_match, edit_match
from apps.players.models import GenderDivision, Player
from apps.ratings import services, tasks
from apps.ratings.models import RatingHistory, RatingState
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}
FIRST_DAY = date(2026, 3, 1)
CORRECTED_DAY = date(2026, 3, 3)


def _member(match: Match) -> User:
    """A member of the verified Organizer of ``match``'s Event."""
    assert match.event is not None
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=match.event.organizer, account=account)
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
    # Hold the replay (#47): it would count the correction before its live
    # update runs, and the live update would then find it unchanged.
    monkeypatch.setattr(
        tasks.replay_ratings, "apply_async", lambda *args, **kwargs: None
    )
    original, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        event=EventFactory.create(
            kind=EventKind.TOURNAMENT, organizer=verified_organizer()
        ),
        status=MatchStatus.PENDING,
        played_on=FIRST_DAY,
    )
    a: Player = players[Team.A][0]
    b: Player = players[Team.B][0]
    member = _member(original)
    with django_capture_on_commit_callbacks(execute=True):
        confirm_match(original, actor=member)
    assert RatingHistory.objects.filter(match=original).count() == 2

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(Match.objects.get(pk=original.pk), actor=b.account)
        correction = edit_match(
            Match.objects.get(pk=original.pk),
            actor=member,
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

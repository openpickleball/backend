"""An edit's duplicate error names the earlier match's code only when the
editor may know it (review finding C, #26).

The rule is ``record_match``'s, applied to the editing actor, not to the
old match's submitter. Made-up data only.
"""

from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, Team
from apps.matches.services import DuplicateMatchError
from apps.matches.tests.factories import MatchFactory
from apps.matches.workflow import edit_match

pytestmark = pytest.mark.django_db


def _edit_into(match: Match, other: Match, actor: Any) -> None:
    """Edit ``match`` into an exact copy of ``other``."""
    teams = {
        team: [p.player for p in other.participants.filter(team=team)] for team in Team
    }
    edit_match(
        match,
        actor=actor,
        format=other.format,
        team_a=teams[Team.A],
        team_b=teams[Team.B],
        games=[(g.team_a_score, g.team_b_score) for g in other.games.all()],
        played_on=other.played_on,
    )


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def test_an_editor_who_may_not_know_the_code_does_not_learn_it(event: Event) -> None:
    submitter = _member(event)
    old: Match = MatchFactory.create(event=event, submitted_by_user=submitter)
    other: Match = MatchFactory.create(event=event, submitted_by_user=submitter)

    with pytest.raises(DuplicateMatchError) as error:
        _edit_into(old, other, _member(event))

    assert error.value.existing_code is None
    assert other.public_code not in str(error.value)


def test_an_editor_who_submitted_the_earlier_match_learns_the_code(
    event: Event,
) -> None:
    editor = _member(event)
    old: Match = MatchFactory.create(event=event)
    other: Match = MatchFactory.create(event=event, submitted_by_user=editor)

    with pytest.raises(DuplicateMatchError) as error:
        _edit_into(old, other, editor)

    assert error.value.existing_code == other.public_code

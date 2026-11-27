"""An edited match still counts toward its player's day (#207).

An edit voids the original and records a correction. The per-player
daily count leaves out voided matches, so it must count the correction
in its original's place: otherwise editing every match and recording
more would reset the count. Made-up data only.
"""

from datetime import date

import pytest
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import FlagRule, Match, MatchFormat
from apps.matches.services import record_match
from apps.matches.workflow import edit_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

DAY = date(2026, 5, 9)
CAP = 2


@pytest.fixture(autouse=True)
def thresholds(settings: Settings) -> None:
    settings.UNUSUAL_SUBMISSION_FIRST_MATCHES = 1
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 1_000
    settings.UNUSUAL_SUBMISSION_DAILY_MATCHES = 1_000
    settings.UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES = CAP


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _record(event: Event, member: User, player: Player, loser: int) -> Match:
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=[player],
        team_b=[PlayerFactory.create()],
        games=((11, loser), (11, loser)),
        played_on=DAY,
        event=event,
        submitted_by_user=member,
    ).match


def _edit(match: Match, member: User, player: Player, loser: int) -> Match:
    opponent = match.participants.exclude(player=player).get().player
    return edit_match(
        match,
        actor=member,
        format=MatchFormat.SINGLES,
        team_a=[player],
        team_b=[opponent],
        games=((11, loser), (11, loser)),
        played_on=DAY,
    )


def _flagged(match: Match) -> bool:
    return match.flags.filter(rule=FlagRule.PLAYER_DAILY_MATCHES).exists()


def test_an_edited_match_still_counts(event: Event, member: User) -> None:
    player = PlayerFactory.create()
    first = _record(event, member, player, 1)
    _record(event, member, player, 2)
    _edit(first, member, player, 3)

    assert _flagged(_record(event, member, player, 4))


def test_editing_every_match_does_not_reset_the_count(
    event: Event, member: User
) -> None:
    player = PlayerFactory.create()
    matches = [_record(event, member, player, loser) for loser in (1, 2)]
    for match, loser in zip(matches, (5, 6), strict=True):
        _edit(match, member, player, loser)

    assert _flagged(_record(event, member, player, 7))

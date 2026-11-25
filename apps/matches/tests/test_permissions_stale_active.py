"""An Account deactivated in the database is refused, even through an
instance loaded while it was active (review finding 2). Made-up data only.
"""

from dataclasses import dataclass

import pytest

from apps.accounts.models import User
from apps.clubs.models import Club, ClubRoleKind, MemberRole
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat
from apps.matches.permissions import (
    MatchPermissionDenied,
    MatchSubmission,
    can_change_match,
    can_create_match,
)
from apps.matches.services import record_match
from apps.matches.tests.factories import MatchFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

SHAPES = ["event", "club", "event_club", "neither"]


@dataclass
class Stale:
    account: User
    event: Event
    club: Club
    player: Player
    other: Player


@pytest.fixture
def stale() -> Stale:
    """An Account with every role that plays, loaded while active, then
    deactivated in the database only."""
    organizer = verified_organizer()
    club = ClubFactory.create()
    player = PlayerFactory.create()
    account: User = player.account
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    ClubRoleFactory.create(club=club, account=account, role=ClubRoleKind.ADMIN)
    # The Club consents, so only is_active refuses the event_club shape.
    ClubOrganizerGrantFactory.create(club=club, organizer=organizer)
    User.objects.filter(pk=account.pk).update(is_active=False)
    assert account.is_active
    return Stale(
        account=account,
        event=EventFactory.create(organizer=organizer),
        club=club,
        player=player,
        other=PlayerFactory.create(),
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_deactivated_account_cannot_create(stale: Stale, shape: str) -> None:
    submission = MatchSubmission(
        event=stale.event if "event" in shape else None,
        club=stale.club if "club" in shape else None,
        player_ids=frozenset({stale.player.pk, stale.other.pk}),
    )

    assert can_create_match(stale.account, submission) is False


@pytest.mark.parametrize("shape", ["event", "club", "event_club"])
def test_deactivated_account_cannot_change(stale: Stale, shape: str) -> None:
    match: Match = MatchFactory.create(
        event=stale.event if "event" in shape else None,
        club=stale.club if "club" in shape else None,
        games=False,
        participants=False,
    )

    assert can_change_match(stale.account, match) is False


def test_record_match_refuses_a_deactivated_account(stale: Stale) -> None:
    before = Match.objects.count()

    with pytest.raises(MatchPermissionDenied):
        record_match(
            format=MatchFormat.SINGLES,
            team_a=[stale.player],
            team_b=[stale.other],
            games=((11, 7),),
            played_on=MatchFactory.build().played_on,
            submitted_by_user=stale.account,
        )

    assert Match.objects.count() == before

"""An edit creates a match, so the editor needs the authority to create it
(review finding B, #26).

``can_change_match`` alone let a Club admin without event authority edit
an Event and Club match, and let an application edit after losing its
Organizer's grant or verification. Since ADR 0021 a Club admin changes no
match at all, and an edit of a match with a Club needs that Club's grant
to the Event's Organizer. A refused editor gets only ``MatchActionDenied``,
before any validation. Made-up data only.
"""

from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Club, ClubOrganizerGrant, ClubRoleKind
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
)
from apps.events.models import Event
from apps.matches.models import Match, MatchStatus, Team
from apps.matches.permissions import MatchActionDenied
from apps.matches.tests.factories import MatchFactory
from apps.matches.workflow import edit_match
from apps.partners.models import OrganizerApplicationGrant, PartnerApplication
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    approved_application,
)

pytestmark = pytest.mark.django_db

NEW_GAMES = ((11, 3),)
BAD_GAMES = ((11, 10),)


def _edit(match: Match, actor: Any, games: tuple[tuple[int, int], ...]) -> Match:
    teams = {
        team: [p.player for p in match.participants.filter(team=team)] for team in Team
    }
    return edit_match(
        match,
        actor=actor,
        format=match.format,
        team_a=teams[Team.A],
        team_b=teams[Team.B],
        games=games,
        played_on=match.played_on,
    )


def _club_admin(club: Club) -> User:
    account: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=account, role=ClubRoleKind.ADMIN)
    return account


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


@pytest.mark.parametrize("games", [NEW_GAMES, BAD_GAMES])
def test_a_club_admin_without_event_authority_cannot_edit(
    event: Event, games: tuple[tuple[int, int], ...]
) -> None:
    club = ClubFactory.create()
    match: Match = MatchFactory.create(event=event, club=club)

    with pytest.raises(MatchActionDenied):
        _edit(match, _club_admin(club), games)

    assert _status(match) == MatchStatus.PENDING


@pytest.mark.parametrize("games", [NEW_GAMES, BAD_GAMES])
def test_a_club_admin_cannot_edit_a_club_only_match(
    games: tuple[tuple[int, int], ...],
) -> None:
    club = ClubFactory.create()
    match: Match = MatchFactory.create(club=club)

    with pytest.raises(MatchActionDenied):
        _edit(match, _club_admin(club), games)

    assert _status(match) == MatchStatus.PENDING


def _granted_application_match(event: Event) -> tuple[Match, PartnerApplication]:
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=event.organizer, application=application
    )
    match: Match = MatchFactory.create(
        by_application=True, submitted_by_application=application, event=event
    )
    return match, application


def test_an_application_with_its_grant_may_edit(event: Event) -> None:
    match, application = _granted_application_match(event)

    corrected = _edit(match, application, NEW_GAMES)

    assert corrected.corrects_id == match.pk


@pytest.mark.parametrize("games", [NEW_GAMES, BAD_GAMES])
def test_an_application_that_lost_its_grant_cannot_edit(
    event: Event, games: tuple[tuple[int, int], ...]
) -> None:
    match, application = _granted_application_match(event)
    OrganizerApplicationGrant.objects.filter(application=application).delete()

    with pytest.raises(MatchActionDenied):
        _edit(match, application, games)

    assert _status(match) == MatchStatus.PENDING


def test_an_application_of_an_unverified_organizer_cannot_edit(event: Event) -> None:
    application = approved_application(organizer=event.organizer)
    match: Match = MatchFactory.create(
        by_application=True, submitted_by_application=application, event=event
    )
    verifier = event.organizer.verified_by
    assert verifier is not None
    unverify_organizer(event.organizer, actor=verifier)

    with pytest.raises(MatchActionDenied):
        _edit(match, application, NEW_GAMES)

    assert _status(match) == MatchStatus.PENDING


@pytest.mark.parametrize("games", [NEW_GAMES, BAD_GAMES])
def test_an_organizer_member_cannot_edit_once_the_club_removed_its_grant(
    event: Event, games: tuple[tuple[int, int], ...]
) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    match: Match = MatchFactory.create(event=event, club=club)
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    ClubOrganizerGrant.objects.filter(club=club).delete()

    with pytest.raises(MatchActionDenied):
        _edit(match, member, games)

    assert _status(match) == MatchStatus.PENDING


def test_an_organizer_member_may_edit_with_the_club_s_grant(event: Event) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    match: Match = MatchFactory.create(event=event, club=club)
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)

    corrected = _edit(match, member, NEW_GAMES)

    assert corrected.corrects_id == match.pk
    assert corrected.club_id == club.pk

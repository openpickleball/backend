"""Who may vouch for a held match, and at which level (#270, ADR 0034).

An Event match is vouched for by its Event's verified Organizer, with the
actors and conflict rule that settle a dispute (``settles_dispute``). A
self-reported match is vouched for by its players. A legacy club-only
match has no vouching level. Made-up data only.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import ClubRoleKind, MemberRole
from apps.clubs.services import unverify_organizer
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
from apps.matches.permissions import VouchLevel, vouch_level
from apps.matches.services import record_match
from apps.matches.tests.factories import MatchFactory
from apps.matches.workflow import edit_match
from apps.partners.tests.factories import approved_application
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (11, 9))
REASON = "Made-up reason for a test."


def _member(event: Event, account: User | None = None) -> User:
    member: User = account or UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return member


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _event_match(players: list[Player], event: Event, **kwargs: object) -> Match:
    if "submitted_by_application" not in kwargs:
        kwargs.setdefault("submitted_by_user", _member(event))
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        event=event,
        **kwargs,  # type: ignore[arg-type]
    ).match


def _self_reported(players: list[Player]) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def test_a_member_vouches_for_an_event_match(
    players: list[Player], event: Event
) -> None:
    match = _event_match(players, event)

    assert vouch_level(_member(event), match) == VouchLevel.ORGANIZER


def test_the_submitting_application_vouches_for_its_event_match(
    players: list[Player], event: Event
) -> None:
    application = approved_application(organizer=event.organizer)
    match = _event_match(players, event, submitted_by_application=application)

    assert vouch_level(application, match) == VouchLevel.ORGANIZER


def test_a_player_vouches_for_a_self_reported_match(players: list[Player]) -> None:
    match = _self_reported(players)

    for player in players:
        assert vouch_level(player.account, match) == VouchLevel.PLAYER


def test_a_participant_of_an_event_match_may_not_vouch(
    players: list[Player], event: Event
) -> None:
    match = _event_match(players, event)

    assert vouch_level(players[0].account, match) is None


def test_a_member_who_plays_in_the_event_match_may_not_vouch(
    players: list[Player], event: Event
) -> None:
    match = _event_match(players, event)

    assert vouch_level(_member(event, players[0].account), match) is None


def test_a_member_who_played_in_the_correction_chain_may_not_vouch(
    players: list[Player], event: Event
) -> None:
    original = _event_match(players, event)
    newcomer = PlayerFactory.create()
    corrected = edit_match(
        original,
        actor=_member(event),
        format=MatchFormat.DOUBLES,
        team_a=[players[0], newcomer],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
    )

    assert vouch_level(_member(event, players[1].account), corrected) is None


def test_a_member_of_another_organizer_may_not_vouch(
    players: list[Player], event: Event
) -> None:
    match = _event_match(players, event)
    other = EventFactory.create(organizer=verified_organizer())

    assert vouch_level(_member(other), match) is None


def test_a_member_of_an_unverified_organizer_may_not_vouch(
    players: list[Player], event: Event
) -> None:
    match = _event_match(players, event)
    member = _member(event)
    verifier: User = UserFactory.create()
    verifier.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    unverify_organizer(event.organizer, actor=verifier, reason=REASON)

    assert vouch_level(member, match) is None


def test_a_moderator_who_is_not_a_member_may_not_vouch(
    players: list[Player], event: Event
) -> None:
    match = _event_match(players, event)
    moderator: User = UserFactory.create()
    moderator.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )

    assert vouch_level(moderator, match) is None
    assert vouch_level(moderator, _self_reported(PlayerFactory.create_batch(4))) is None


def test_a_club_admin_may_not_vouch(players: list[Player], event: Event) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    match = _event_match(players, event, club=club)
    club_admin: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=club_admin, role=ClubRoleKind.ADMIN)

    assert vouch_level(club_admin, match) is None


def test_a_non_participant_may_not_vouch_for_a_self_reported_match(
    players: list[Player], event: Event
) -> None:
    match = _self_reported(players)

    assert vouch_level(PlayerFactory.create().account, match) is None
    assert vouch_level(_member(event), match) is None


def test_nobody_vouches_for_a_legacy_club_only_match(event: Event) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    match: Match = MatchFactory.create(club=club)
    participant = match.participants.first()
    assert participant is not None
    club_admin: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=club_admin, role=ClubRoleKind.ADMIN)

    for actor in (_member(event), club_admin, participant.player.account):
        assert vouch_level(actor, match) is None


def test_an_inactive_player_may_not_vouch(players: list[Player]) -> None:
    match = _self_reported(players)
    account = players[0].account
    account.is_active = False
    account.save(update_fields=["is_active"])

    assert vouch_level(account, match) is None


def test_an_unsaved_match_is_refused(players: list[Player]) -> None:
    assert vouch_level(players[0].account, Match()) is None

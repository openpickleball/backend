"""Who may confirm and dispute a Match, and who is a moderator (#26).

A moderator only voids matches in bulk (#273). Made-up data only.
"""

import pytest
from django.contrib.auth.models import AnonymousUser, Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import ClubRoleKind, MemberRole
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match
from apps.matches.permissions import (
    MatchActionDenied,
    can_confirm_as_organizer,
    is_moderator,
    participant_player,
)
from apps.matches.tests.factories import MatchFactory
from apps.partners.models import OrganizerApplicationGrant
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    PartnerApplicationFactory,
    approved_application,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def match() -> Match:
    made: Match = MatchFactory.create()
    return made


def _event_match(*, verified: bool = True) -> Match:
    organizer = verified_organizer() if verified else OrganizerFactory.create()
    made: Match = MatchFactory.create(event=EventFactory.create(organizer=organizer))
    return made


def _moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


# participant_player


def test_a_participant_gets_their_player(match: Match) -> None:
    player = match.participants.first().player  # type: ignore[union-attr]

    assert participant_player(player.account, match) == player


def test_another_account_is_not_a_participant(match: Match) -> None:
    assert participant_player(UserFactory.create(), match) is None


def test_an_inactive_participant_is_refused(match: Match) -> None:
    account = match.participants.first().player.account  # type: ignore[union-attr]
    User.objects.filter(pk=account.pk).update(is_active=False)

    assert participant_player(account, match) is None


def test_an_application_is_never_a_participant(match: Match) -> None:
    assert participant_player(approved_application(), match) is None


def test_anonymous_is_never_a_participant(match: Match) -> None:
    assert participant_player(AnonymousUser(), match) is None


def test_an_unsaved_match_has_no_participants(match: Match) -> None:
    player = match.participants.first().player  # type: ignore[union-attr]

    assert participant_player(player.account, MatchFactory.build()) is None


# can_confirm_as_organizer


@pytest.mark.parametrize("role", list(MemberRole))
def test_a_member_of_the_verified_organizer_may_confirm(role: MemberRole) -> None:
    match = _event_match()
    assert match.event is not None
    account = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=match.event.organizer, account=account, role=role
    )

    assert can_confirm_as_organizer(account, match)


def test_a_member_of_an_unverified_organizer_may_not() -> None:
    match = _event_match(verified=False)
    assert match.event is not None
    account = UserFactory.create()
    OrganizerMemberFactory.create(organizer=match.event.organizer, account=account)

    assert not can_confirm_as_organizer(account, match)


def test_the_owning_approved_application_may_confirm() -> None:
    match = _event_match()
    assert match.event is not None

    assert can_confirm_as_organizer(
        approved_application(organizer=match.event.organizer), match
    )


def test_an_unapproved_owning_application_may_not() -> None:
    match = _event_match()
    assert match.event is not None
    application = PartnerApplicationFactory.create(organizer=match.event.organizer)

    assert not can_confirm_as_organizer(application, match)


def test_a_club_admin_without_event_authority_may_not() -> None:
    club = ClubFactory.create()
    match: Match = MatchFactory.create(club=club)
    account = UserFactory.create()
    ClubRoleFactory.create(club=club, account=account, role=ClubRoleKind.ADMIN)

    assert not can_confirm_as_organizer(account, match)


def test_a_granted_approved_application_may_confirm() -> None:
    # ADR 0021: an Organizer Application Grant acts like ownership.
    match = _event_match()
    assert match.event is not None
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=match.event.organizer, application=application
    )

    assert can_confirm_as_organizer(application, match)
    OrganizerApplicationGrant.objects.filter(application=application).delete()
    assert not can_confirm_as_organizer(application, match)


def test_an_unverified_organizer_s_granted_application_may_not() -> None:
    match = _event_match(verified=False)
    assert match.event is not None
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=match.event.organizer, application=application
    )

    assert not can_confirm_as_organizer(application, match)


def test_an_application_granted_by_another_organizer_may_not() -> None:
    match = _event_match()
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=verified_organizer(), application=application
    )

    assert not can_confirm_as_organizer(application, match)


def test_no_one_confirms_as_organizer_without_an_event(match: Match) -> None:
    assert not can_confirm_as_organizer(match.submitted_by_user, match)  # type: ignore[arg-type]


# is_moderator (the bulk void's rule)


def test_an_account_with_the_permission_is_a_moderator() -> None:
    assert is_moderator(_moderator())


def test_an_account_without_the_permission_is_not() -> None:
    assert not is_moderator(UserFactory.create())


def test_an_inactive_moderator_is_not() -> None:
    account = _moderator()
    User.objects.filter(pk=account.pk).update(is_active=False)

    assert not is_moderator(account)


def test_a_permission_removed_after_loading_is_not_kept() -> None:
    account = _moderator()
    assert is_moderator(account)
    account.user_permissions.clear()

    assert not is_moderator(account)


@pytest.mark.parametrize(
    "actor", [lambda: AnonymousUser(), lambda: approved_application()]
)
def test_others_are_never_moderators(actor: object) -> None:
    assert not is_moderator(actor())  # type: ignore[operator]


def test_the_refusal_message_is_fixed() -> None:
    assert str(MatchActionDenied()) == "You are not allowed to change this match."

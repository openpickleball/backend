"""One read of the Event decides both authority and club consent (ADR 0021).

If staff move an Event to another Organizer while a submission is being
checked, the actor's authority and the Club's consent must both be judged
against the same Organizer. Otherwise a member of the old Organizer passes
authority there, and the new Organizer's Club Organizer Grant passes
consent, so a Club that never granted the actor's Organizer is named.
Made-up data only.
"""

from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Club, Organizer, OrganizerMember
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import permissions
from apps.matches.permissions import MatchSubmission, can_create_match
from apps.partners.models import OrganizerApplicationGrant
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    approved_application,
)

pytestmark = pytest.mark.django_db


class _MovingManager:
    """Moves the Event to ``target`` just before the real lookup runs."""

    def __init__(self, real: Any, event: Event, target: Organizer) -> None:
        self._real = real
        self._event = event
        self._target = target

    def filter(self, *args: Any, **kwargs: Any) -> Any:
        Event.objects.filter(pk=self._event.pk).update(organizer=self._target)
        return self._real.filter(*args, **kwargs)


class _MovingModel:
    def __init__(self, real: Any, event: Event, target: Organizer) -> None:
        self.objects = _MovingManager(real.objects, event, target)


@pytest.fixture
def setting() -> tuple[Event, Organizer, Club]:
    """An Event of verified O1, a verified O2, and a Club that granted only O2."""
    first = verified_organizer()
    second = verified_organizer()
    club: Club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=second)
    event: Event = EventFactory.create(organizer=first)
    return event, second, club


def test_a_member_is_judged_against_one_read_of_the_event(
    setting: tuple[Event, Organizer, Club], monkeypatch: pytest.MonkeyPatch
) -> None:
    event, second, club = setting
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    monkeypatch.setattr(
        permissions,
        "OrganizerMember",
        _MovingModel(OrganizerMember, event, second),
    )

    allowed = can_create_match(
        account, MatchSubmission(event=event, club=club, player_ids=frozenset())
    )

    assert not allowed


def test_a_granted_application_is_judged_against_one_read_of_the_event(
    setting: tuple[Event, Organizer, Club], monkeypatch: pytest.MonkeyPatch
) -> None:
    event, second, club = setting
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=event.organizer, application=application
    )
    monkeypatch.setattr(
        permissions,
        "OrganizerApplicationGrant",
        _MovingModel(OrganizerApplicationGrant, event, second),
    )

    allowed = can_create_match(
        application, MatchSubmission(event=event, club=club, player_ids=frozenset())
    )

    assert not allowed


def test_an_unsaved_club_is_refused() -> None:
    organizer = verified_organizer()
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=account)
    event: Event = EventFactory.create(organizer=organizer)

    unsaved = Club(name="Made-up Unsaved Club")

    assert not can_create_match(
        account, MatchSubmission(event=event, club=unsaved, player_ids=frozenset())
    )

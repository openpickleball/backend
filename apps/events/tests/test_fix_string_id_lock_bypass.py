"""A locked field given as a string id is still checked (#163 review).

Django casts ``"5"`` to ``5`` when it writes a foreign key, so
``Event.save()`` must compare the converted value, never skip a value
that is not an int. On an Event with a Match, a string id for another
organizer or club is refused through a plain save and through
``save_event``, and nothing is written or audited. Made-up data only.
"""

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.models import Club
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerFactory,
)
from apps.events.models import Event
from apps.events.services import save_event
from apps.events.tests.factories import EventFactory
from apps.matches.tests.factories import MatchFactory

pytestmark = pytest.mark.django_db


def _granted_club(event: Event) -> Club:
    club: Club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    return club


def _event_with_match() -> Event:
    event: Event = EventFactory.create()
    event.club = _granted_club(event)
    event.save()
    MatchFactory.create(event=event)
    return Event.objects.get(pk=event.pk)


def _save(event: Event, *, through_service: bool) -> None:
    if through_service:
        save_event(event, actor=UserFactory.create(is_staff=True))
    else:
        event.save()


def _stored(event: Event) -> tuple[int, int | None]:
    stored: tuple[int, int | None] = Event.objects.values_list(
        "organizer_id", "club_id"
    ).get(pk=event.pk)
    return stored


@pytest.mark.parametrize("through_service", [False, True])
def test_a_string_organizer_id_is_refused(through_service: bool) -> None:
    event = _event_with_match()
    before = _stored(event)
    other = OrganizerFactory.create()
    # The new organizer has the club's grant, so only the lock refuses.
    ClubOrganizerGrantFactory.create(club=event.club, organizer=other)
    event.organizer_id = str(other.pk)

    with pytest.raises(ValidationError) as caught:
        _save(event, through_service=through_service)

    assert "organizer" in caught.value.message_dict
    assert _stored(event) == before
    assert not AuditEvent.objects.filter(target_object_id=str(event.pk)).exists()


@pytest.mark.parametrize("through_service", [False, True])
def test_a_string_club_id_is_refused(through_service: bool) -> None:
    event = _event_with_match()
    before = _stored(event)
    event.club_id = str(_granted_club(event).pk)

    with pytest.raises(ValidationError) as caught:
        _save(event, through_service=through_service)

    assert "club" in caught.value.message_dict
    assert _stored(event) == before
    assert not AuditEvent.objects.filter(target_object_id=str(event.pk)).exists()

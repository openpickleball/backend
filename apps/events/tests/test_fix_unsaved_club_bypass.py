"""A Club assigned unsaved, then saved, is still checked (#163 review).

Django copies ``club_id`` from a cached related Club only when the Event is
saved, so a check that reads ``club_id`` earlier sees None. The lock and
the Club Organizer Grant rule (ADR 0021) must see the real id: an Event
with matches and no Club refuses it, and a new Event whose Club has no
grant is refused. Nothing is written or audited. Made-up data only.
"""

from datetime import date

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.models import Club
from apps.clubs.tests.factories import OrganizerFactory
from apps.events.models import Event, EventKind
from apps.events.services import save_event
from apps.events.tests.factories import EventFactory
from apps.matches.tests.factories import MatchFactory

pytestmark = pytest.mark.django_db

LOCKED = "This event has matches, so its club cannot change."
NOT_GRANTED = "This club has not allowed the organizer to name it."


def _assign_unsaved_club_then_save_it(event: Event) -> Club:
    club = Club(name="Made-up Court Club")
    event.club = club
    club.save()
    return club


def _event_with_match_and_no_club() -> Event:
    event: Event = EventFactory.create()
    MatchFactory.create(event=event)
    return Event.objects.get(pk=event.pk)


def _stored_club_id(event: Event) -> int | None:
    stored: int | None = Event.objects.values_list("club_id", flat=True).get(
        pk=event.pk
    )
    return stored


def test_a_plain_save_refuses_the_club() -> None:
    event = _event_with_match_and_no_club()
    _assign_unsaved_club_then_save_it(event)

    with pytest.raises(ValidationError) as caught:
        event.save()

    assert caught.value.message_dict == {"club": [LOCKED]}
    assert _stored_club_id(event) is None


def test_save_event_refuses_the_club_and_records_nothing() -> None:
    event = _event_with_match_and_no_club()
    _assign_unsaved_club_then_save_it(event)

    with pytest.raises(ValidationError) as caught:
        save_event(event, actor=UserFactory.create(is_staff=True))

    assert "club" in caught.value.message_dict
    assert _stored_club_id(event) is None
    assert not AuditEvent.objects.filter(target_object_id=str(event.pk)).exists()


def test_a_new_event_with_an_ungranted_club_is_refused() -> None:
    event = Event(
        name="Made-up Summer Open",
        kind=EventKind.TOURNAMENT,
        organizer=OrganizerFactory.create(),
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 2),
    )
    _assign_unsaved_club_then_save_it(event)

    with pytest.raises(ValidationError) as caught:
        save_event(event, actor=UserFactory.create(is_staff=True))

    assert caught.value.message_dict == {"club": [NOT_GRANTED]}
    assert not Event.objects.exists()
    assert not AuditEvent.objects.exists()

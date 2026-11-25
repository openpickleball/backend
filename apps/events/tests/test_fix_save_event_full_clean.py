"""``save_event`` runs every model rule before it saves (#163 review).

The admin form cleans the Event, but another caller (#125's API) may not,
so ``save_event`` runs ``full_clean()`` itself: a Club without a Club
Organizer Grant (ADR 0021) or dates out of order are refused with
``ValidationError`` and nothing is saved or recorded. Made-up data only.
"""

from datetime import date

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.tests.factories import ClubFactory, OrganizerFactory
from apps.events.models import Event, EventKind
from apps.events.services import save_event
from apps.events.tests.factories import EventFactory

pytestmark = pytest.mark.django_db


def test_a_club_without_a_grant_is_refused_and_nothing_is_recorded() -> None:
    event = Event(
        name="Made-up Club Cup",
        kind=EventKind.TOURNAMENT,
        organizer=OrganizerFactory.create(),
        club=ClubFactory.create(),
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
    )

    with pytest.raises(ValidationError) as caught:
        save_event(event, actor=UserFactory.create(is_staff=True))

    assert "club" in caught.value.message_dict
    assert not Event.objects.exists()
    assert not AuditEvent.objects.exists()


def test_dates_out_of_order_on_a_change_are_refused_and_nothing_is_recorded() -> None:
    event: Event = EventFactory.create()
    stored_end = event.end_date
    event.end_date = date(event.start_date.year - 1, 1, 1)

    with pytest.raises(ValidationError) as caught:
        save_event(event, actor=UserFactory.create(is_staff=True))

    assert "end_date" in caught.value.message_dict
    assert Event.objects.get(pk=event.pk).end_date == stored_end
    assert not AuditEvent.objects.exists()

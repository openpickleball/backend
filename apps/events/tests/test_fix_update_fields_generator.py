"""``Event.save()`` reads a one-pass ``update_fields`` only once (#163 review).

A generator for ``update_fields`` must reach Django whole: a stale
instance saving only its name must write only the name, never its old
organizer over another session's change. Made-up data only.
"""

import pytest
from django.db.models import QuerySet

from apps.clubs.tests.factories import OrganizerFactory
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.tests.factories import MatchFactory

pytestmark = pytest.mark.django_db


def test_a_generator_of_update_fields_saves_only_those_fields() -> None:
    stale: Event = EventFactory.create()
    new = OrganizerFactory.create()
    QuerySet(model=Event).filter(pk=stale.pk).update(organizer=new)
    MatchFactory.create(event=Event.objects.get(pk=stale.pk))
    stale.name = "Made-up Renamed League"

    stale.save(update_fields=(name for name in ["name"]))

    stored = Event.objects.get(pk=stale.pk)
    assert stored.name == "Made-up Renamed League"
    assert stored.organizer_id == new.pk

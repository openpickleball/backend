"""Factories for Events. Made-up data only."""

from datetime import date, timedelta

from factory.declarations import LazyAttribute, LazyFunction, SubFactory
from factory.django import DjangoModelFactory
from faker import Faker

from apps.clubs.tests.factories import OrganizerFactory
from apps.events.models import Event, EventKind

_faker = Faker()


def _event_name() -> str:
    return f"{_faker.city()} {_faker.word()} League"[:200]


# A match with an Event must be played within its dates (#207), so the
# default span starts long before and ends long after any date a test
# plays a match on. A test that needs other dates passes them.
_EARLIEST_START = date(2000, 1, 1)
_SPAN_DAYS = 36_500


def _start_date() -> date:
    return _EARLIEST_START + timedelta(days=_faker.random_int(min=0, max=1_800))


def _end_date(event: Event) -> date:
    return event.start_date + timedelta(
        days=_SPAN_DAYS + _faker.random_int(min=0, max=90)
    )


class EventFactory(DjangoModelFactory[Event]):
    """A saved league Event with no Club, its dates in order and wide."""

    class Meta:
        model = Event

    # factory_boy's declarations carry no type hints.
    name = LazyFunction(_event_name)  # type: ignore[no-untyped-call]
    kind = EventKind.LEAGUE
    organizer = SubFactory(OrganizerFactory)  # type: ignore[no-untyped-call]
    start_date = LazyFunction(_start_date)  # type: ignore[no-untyped-call]
    end_date = LazyAttribute(_end_date)  # type: ignore[no-untyped-call]

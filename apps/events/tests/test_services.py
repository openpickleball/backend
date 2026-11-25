"""Event saves and deletes record Audit Events (#163, ADR 0035).

``save_event`` records one ``event.create`` or one ``event.change`` with
only the changed fields, or nothing when nothing changed or the save
raises. ``delete_event`` records ``event.delete`` before the delete, and a
refused delete records nothing. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import Club, Organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerFactory,
)
from apps.events.models import Event, EventKind
from apps.events.services import delete_event, save_event
from apps.events.tests.factories import EventFactory
from apps.matches.tests.factories import MatchFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def actor() -> User:
    account: User = UserFactory.create(is_staff=True)
    return account


def _new_event(organizer: Organizer, club: Club | None = None) -> Event:
    return Event(
        name="Made-up Spring League",
        kind=EventKind.LEAGUE,
        organizer=organizer,
        club=club,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 5, 31),
    )


def _events(event: Event) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(target_object_id=str(event.pk)).order_by("pk")
    )


def _values(event: Event, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": event.name,
        "kind": str(event.kind),
        "organizer": event.organizer.public_code,
        "club": None if event.club is None else event.club.public_code,
        "start_date": event.start_date.isoformat(),
        "end_date": event.end_date.isoformat(),
    }
    values.update(overrides)
    return values


def test_a_new_event_records_one_create(actor: User) -> None:
    organizer: Organizer = OrganizerFactory.create()
    club: Club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=organizer)
    event = _new_event(organizer, club)

    save_event(event, actor=actor)

    [audit] = _events(event)
    assert audit.action == AuditAction.EVENT_CREATE
    assert audit.actor_user == actor
    assert audit.target == event
    assert audit.before is None
    assert audit.after == {
        "name": "Made-up Spring League",
        "kind": "league",
        "organizer": organizer.public_code,
        "club": club.public_code,
        "start_date": "2026-03-01",
        "end_date": "2026-05-31",
    }


def test_a_change_records_only_the_changed_fields(actor: User) -> None:
    event: Event = EventFactory.create(kind=EventKind.LEAGUE)
    MatchFactory.create(event=event)
    old_end = event.end_date.isoformat()
    event.kind = EventKind.TOURNAMENT
    event.end_date = date(event.end_date.year + 1, 1, 1)

    save_event(event, actor=actor)

    [audit] = _events(event)
    assert audit.action == AuditAction.EVENT_CHANGE
    assert audit.actor_user == actor
    assert audit.before == {"kind": "league", "end_date": old_end}
    assert audit.after == {
        "kind": "tournament",
        "end_date": event.end_date.isoformat(),
    }


def test_an_organizer_change_records_the_open_codes(actor: User) -> None:
    event: Event = EventFactory.create()
    old = event.organizer.public_code
    new: Organizer = OrganizerFactory.create()
    event.organizer = new

    save_event(event, actor=actor)

    [audit] = _events(event)
    assert audit.before == {"organizer": old}
    assert audit.after == {"organizer": new.public_code}


def test_the_organizer_code_is_read_from_the_database(actor: User) -> None:
    event: Event = EventFactory.create()
    stored_code = event.organizer.public_code
    # A stale related instance with another code never reaches the event.
    event.organizer.public_code = "O-STAL-ESTA-LEXX"

    save_event(event, actor=actor)

    assert _events(event) == []
    event.name = "Made-up New Name"
    save_event(event, actor=actor)
    [audit] = _events(event)
    assert audit.after == {"name": "Made-up New Name"}
    assert Event.objects.get(pk=event.pk).organizer.public_code == stored_code


def test_a_save_that_changes_nothing_records_nothing(actor: User) -> None:
    event: Event = EventFactory.create()

    save_event(event, actor=actor)

    assert _events(event) == []


def test_a_refused_organizer_change_records_nothing(actor: User) -> None:
    event: Event = EventFactory.create()
    MatchFactory.create(event=event)
    event.organizer = OrganizerFactory.create()

    with pytest.raises(ValidationError):
        save_event(event, actor=actor)

    assert _events(event) == []


def test_a_save_the_database_refuses_records_nothing(actor: User) -> None:
    event: Event = EventFactory.create()
    event.end_date = date(event.start_date.year - 1, 1, 1)

    with transaction.atomic(), pytest.raises(IntegrityError):
        save_event(event, actor=actor)

    assert _events(event) == []


def test_a_delete_records_the_values_then_deletes(actor: User) -> None:
    event: Event = EventFactory.create()
    expected = _values(event)
    pk = event.pk

    delete_event(event, actor=actor)

    assert not Event.objects.filter(pk=pk).exists()
    [audit] = AuditEvent.objects.filter(target_object_id=str(pk))
    assert audit.action == AuditAction.EVENT_DELETE
    assert audit.actor_user == actor
    assert audit.before == expected
    assert audit.after is None


def test_a_refused_delete_records_nothing(actor: User) -> None:
    event: Event = EventFactory.create()
    MatchFactory.create(event=event)

    with pytest.raises(ProtectedError):
        delete_event(event, actor=actor)

    assert Event.objects.filter(pk=event.pk).exists()
    assert _events(event) == []

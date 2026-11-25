"""An Event's organizer cannot change once a Match names it (#163, ADR 0035).

The organizer decides who may submit the Event's counted matches, so a
change after matches exist would hand them to another Organizer. The kind,
name, and dates stay changeable. Made-up data only.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerFactory
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchStatus
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.workflow import void_match

pytestmark = pytest.mark.django_db

LOCKED = "This event has matches, so its organizer cannot change."


def _event_with_match(status: str = MatchStatus.CONFIRMED) -> Event:
    event: Event = EventFactory.create()
    match = MatchFactory.create(event=event)
    if status != MatchStatus.PENDING:
        raw_update(match, status=status)
    return event


def _stored_organizer_id(event: Event) -> int:
    stored: int = Event.objects.values_list("organizer_id", flat=True).get(pk=event.pk)
    return stored


@pytest.mark.parametrize("status", MatchStatus.values)
def test_save_refuses_an_organizer_change_once_a_match_names_the_event(
    status: str,
) -> None:
    event = _event_with_match(status)
    old = event.organizer_id
    event.organizer = OrganizerFactory.create()

    with pytest.raises(ValidationError) as caught:
        event.save()

    assert caught.value.message_dict == {"organizer": [LOCKED]}
    assert _stored_organizer_id(event) == old


def test_save_allows_an_organizer_change_with_no_match() -> None:
    event: Event = EventFactory.create()
    new = OrganizerFactory.create()
    event.organizer = new

    event.save()

    assert _stored_organizer_id(event) == new.pk


def test_the_kind_name_and_dates_stay_changeable() -> None:
    event = _event_with_match()
    event.kind = EventKind.TOURNAMENT
    event.name = "Made-up Renamed Open"
    event.start_date -= timedelta(days=1)
    event.end_date += timedelta(days=1)

    event.save()

    event.refresh_from_db()
    assert (event.kind, event.name) == (EventKind.TOURNAMENT, "Made-up Renamed Open")


def test_an_organizer_change_locks_the_event_row_for_update() -> None:
    event: Event = EventFactory.create()
    event.organizer = OrganizerFactory.create()

    with CaptureQueriesContext(connection) as queries:
        event.save()

    locks = [q["sql"] for q in queries if "FOR" in q["sql"] and "UPDATE" in q["sql"]]
    assert locks
    # Plain FOR UPDATE conflicts with a Match insert's FOR KEY SHARE.
    assert all("FOR UPDATE" in sql and "NO KEY" not in sql for sql in locks)


def test_a_save_with_the_same_organizer_still_locks_the_row() -> None:
    event = _event_with_match()
    event.name = "Made-up Other Name"

    with CaptureQueriesContext(connection) as queries:
        event.save()

    locks = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
    assert locks
    assert all("NO KEY" not in sql for sql in locks)


def test_an_event_whose_only_match_was_voided_still_refuses() -> None:
    event: Event = EventFactory.create()
    match = MatchFactory.create(event=event)
    moderator = UserFactory.create()
    moderator.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    void_match(match, actor=moderator, reason="Made-up void")
    assert Match.objects.get(pk=match.pk).status == MatchStatus.VOIDED
    old = event.organizer_id
    event.organizer = OrganizerFactory.create()

    with pytest.raises(ValidationError) as caught:
        event.save()

    assert caught.value.message_dict == {"organizer": [LOCKED]}
    assert _stored_organizer_id(event) == old


def test_full_clean_reports_the_same_message() -> None:
    event = _event_with_match()
    event.organizer = OrganizerFactory.create()

    with pytest.raises(ValidationError) as caught:
        event.full_clean()

    assert caught.value.message_dict["organizer"] == [LOCKED]


def test_the_admin_form_shows_the_message(client: Client) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    event = _event_with_match()
    other = OrganizerFactory.create()

    response = client.post(
        reverse("admin:events_event_change", args=[event.pk]),
        {
            "name": event.name,
            "kind": event.kind,
            "organizer": str(other.pk),
            "club": "",
            "start_date": event.start_date.isoformat(),
            "end_date": event.end_date.isoformat(),
        },
    )

    assert response.status_code == 200
    assert response.context["adminform"].form.errors["organizer"] == [LOCKED]
    assert _stored_organizer_id(event) == event.organizer_id

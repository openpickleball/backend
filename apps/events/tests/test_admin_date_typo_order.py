"""A date typo on the Event change form reports only the typo.

The form leaves a field that failed at its stored value, and Django still
runs ``clean()``, so the order check compared the other new date with the
stored one and reported a false order error.
"""

from datetime import date
from typing import TYPE_CHECKING

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.events.models import Event
from apps.events.tests.factories import EventFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

STORED_START = date(2026, 9, 5)
STORED_END = date(2026, 9, 6)


@pytest.mark.parametrize(
    ("start_date", "end_date", "bad_field", "good_field"),
    [
        ("not-a-date", "2026-08-02", "start_date", "end_date"),
        ("2026-10-01", "not-a-date", "end_date", "start_date"),
    ],
)
def test_change_form_date_typo_reports_only_the_typo(
    client: Client, start_date: str, end_date: str, bad_field: str, good_field: str
) -> None:
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(account)
    event: Event = EventFactory.create(start_date=STORED_START, end_date=STORED_END)

    response = client.post(
        reverse("admin:events_event_change", args=[event.pk]),
        {
            "name": event.name,
            "kind": event.kind,
            "organizer": str(event.organizer.pk),
            "club": "",
            "start_date": start_date,
            "end_date": end_date,
        },
    )

    assert response.status_code == 200
    errors = response.context["adminform"].form.errors
    assert errors[bad_field] == ["Enter a valid date."]
    assert good_field not in errors
    event.refresh_from_db()
    assert (event.start_date, event.end_date) == (STORED_START, STORED_END)

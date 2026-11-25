"""A ``ValueError`` from ``save_event`` is never a 500 (#125).

``save_event`` refuses a related record or Event that is not stored with
``ValueError``. Create answers it with the fixed 409, PATCH with the
unknown code's 404. Made-up data only.
"""

from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.models import Organizer
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events import api as events_api
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory

pytestmark = pytest.mark.django_db

EVENTS = "/api/v1/organizer/events"
NOT_SAVED_TEXT = "The event could not be saved. Try again."


def _refuse(event: Event, *, actor: Any) -> None:
    raise ValueError("made-up unsaved related object")


def _member_client(organizer: Organizer) -> Client:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=account)
    client = Client()
    client.force_login(account)
    return client


def test_create_value_error_is_the_fixed_409(monkeypatch: pytest.MonkeyPatch) -> None:
    organizer = verified_organizer()
    client = _member_client(organizer)
    monkeypatch.setattr(events_api, "save_event", _refuse)

    response = client.post(
        EVENTS,
        {
            "organizer": organizer.public_code,
            "name": "Made-up Value Cup",
            "kind": "league",
            "start_date": "2026-03-01",
            "end_date": "2026-03-02",
        },
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "conflict",
        "message": NOT_SAVED_TEXT,
        "errors": [],
    }
    assert not Event.objects.exists()


def test_patch_value_error_is_the_unknown_404(monkeypatch: pytest.MonkeyPatch) -> None:
    organizer = verified_organizer()
    event = EventFactory.create(organizer=organizer, kind=EventKind.LEAGUE)
    client = _member_client(organizer)
    unknown = client.patch(
        f"{EVENTS}/E-0000-0000", {"kind": "league"}, content_type="application/json"
    )
    monkeypatch.setattr(events_api, "save_event", _refuse)

    response = client.patch(
        f"{EVENTS}/{event.public_code}",
        {"kind": "tournament"},
        content_type="application/json",
    )

    assert response.status_code == 404
    assert response.content == unknown.content
    assert Event.objects.get(pk=event.pk).kind == EventKind.LEAGUE
    assert not AuditEvent.objects.filter(action="event.change").exists()

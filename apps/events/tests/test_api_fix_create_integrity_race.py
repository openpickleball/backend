"""A create whose commit fails a deferred foreign key gets a fixed 409 (#125).

Foreign keys are checked at commit, so an Organizer or Club deleted after
``save_event``'s checks raises ``IntegrityError`` when the view's
transaction ends. The answer is the same fixed 409 as any other create
that could not be saved, never a 500. Made-up data only.
"""

from typing import Any

import pytest
from django.db import IntegrityError
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events import api as events_api
from apps.events.models import Event

pytestmark = pytest.mark.django_db

EVENTS = "/api/v1/organizer/events"
NOT_SAVED_TEXT = "The event could not be saved. Try again."


def test_create_whose_commit_fails_integrity_is_a_fixed_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organizer = verified_organizer()
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )

    def fail_at_commit(event: Event, *, actor: Any) -> None:
        raise IntegrityError("made-up deferred foreign key violation")

    monkeypatch.setattr(events_api, "save_event", fail_at_commit)
    client = Client()
    client.force_login(account)

    response = client.post(
        EVENTS,
        {
            "organizer": organizer.public_code,
            "name": "Made-up Race Cup",
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
    assert "made-up deferred" not in response.content.decode()
    assert not Event.objects.exists()

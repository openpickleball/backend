"""A refused actor's change or delete takes no lock on the Event (#125).

``FOR UPDATE`` on an Event blocks its Organizer's in-flight match
submissions (``hold_event`` takes ``FOR KEY SHARE``), so only an actor
who may manage the Event takes it. Anyone else gets the unknown code's
404 before any lock. Made-up data only.
"""

from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory

pytestmark = pytest.mark.django_db

EVENTS = "/api/v1/organizer/events"


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _refused() -> list[tuple[User, Event]]:
    verified_event = EventFactory.create(organizer=verified_organizer())
    unverified = OrganizerFactory.create()
    unverified_event = EventFactory.create(organizer=unverified)
    unverified_member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=unverified, account=unverified_member)
    other_member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=verified_organizer(), account=other_member)
    return [
        (UserFactory.create(), verified_event),
        (other_member, verified_event),
        (unverified_member, unverified_event),
    ]


def _locks(client: Client, method: str, path: str, **kwargs: Any) -> list[str]:
    """The ``FOR UPDATE`` queries a request ran; the request gets 404."""
    with CaptureQueriesContext(connection) as queries:
        response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 404
    return [q["sql"] for q in queries.captured_queries if "FOR UPDATE" in q["sql"]]


def test_refused_patch_takes_no_lock() -> None:
    for account, event in _refused():
        locks = _locks(
            _client(account),
            "patch",
            f"{EVENTS}/{event.public_code}",
            data={"kind": "tournament"},
            content_type="application/json",
        )
        assert locks == []


def test_refused_delete_takes_no_lock() -> None:
    for account, event in _refused():
        locks = _locks(_client(account), "delete", f"{EVENTS}/{event.public_code}")
        assert locks == []

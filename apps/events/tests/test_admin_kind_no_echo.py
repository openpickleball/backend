"""The Event add form refuses an unknown kind without echoing it.

Django's form-level ``invalid_choice`` message names the submitted value,
and the model field's ``error_messages`` do not reach the admin form.
"""

from typing import TYPE_CHECKING

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerFactory
from apps.events.models import Event

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("kind", ["self_reported", "zqxjmarker"])
def test_add_form_refuses_unknown_kind_without_echo(client: Client, kind: str) -> None:
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(account)
    organizer = OrganizerFactory.create()

    response = client.post(
        reverse("admin:events_event_add"),
        {
            "name": "Made-up Fall Open",
            "kind": kind,
            "organizer": str(organizer.pk),
            "club": "",
            "start_date": "2026-09-05",
            "end_date": "2026-09-06",
        },
    )

    assert response.status_code == 200
    errors = response.context["adminform"].form.errors
    assert "kind" in errors
    assert kind not in " ".join(errors["kind"])
    assert kind not in response.content.decode()
    assert not Event.objects.exists()

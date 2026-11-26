"""A tampered non-numeric autocomplete id gets a form error, not a 500.

Issue #161: the form refuses the value with fixed text, but rendering the
autocomplete widget again filtered the related model on the raw value,
and an integer key raised ``ValueError``.
"""

from typing import TYPE_CHECKING

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole, Organizer, OrganizerMember
from apps.events.models import Event

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

MARKER = "zqxj"
VALUES = ["zqxjmarker", "1.5", "zqxj\x00marker"]


def _staff_client(client: Client) -> Client:
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(account)
    return client


@pytest.mark.parametrize("value", VALUES)
def test_organizer_member_inline_refuses_non_numeric_account(
    client: Client, value: str
) -> None:
    response = _staff_client(client).post(
        reverse("admin:clubs_organizer_add"),
        {
            "name": "Made-up Organizer",
            "members-TOTAL_FORMS": "1",
            "members-INITIAL_FORMS": "0",
            "members-MIN_NUM_FORMS": "0",
            "members-MAX_NUM_FORMS": "1000",
            "members-0-id": "",
            "members-0-account": value,
            "members-0-role": MemberRole.ADMIN,
        },
    )

    assert response.status_code == 200
    formset = response.context["inline_admin_formsets"][0].formset
    errors = formset.forms[0].errors
    assert "account" in errors
    assert value not in " ".join(errors["account"])
    assert MARKER not in response.content.decode()
    assert not Organizer.objects.exists()
    assert not OrganizerMember.objects.exists()


@pytest.mark.parametrize("value", VALUES)
def test_event_form_refuses_non_numeric_organizer_and_club(
    client: Client, value: str
) -> None:
    response = _staff_client(client).post(
        reverse("admin:events_event_add"),
        {
            "name": "Made-up Fall Open",
            "kind": "rec_verified",
            "organizer": value,
            "club": value,
            "start_date": "2026-09-05",
            "end_date": "2026-09-06",
        },
    )

    assert response.status_code == 200
    errors = response.context["adminform"].form.errors
    for field in ("organizer", "club"):
        assert field in errors
        assert value not in " ".join(errors[field])
    assert MARKER not in response.content.decode()
    assert not Event.objects.exists()

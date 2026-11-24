"""The admin lists, adds, and changes Events, and refuses bad input."""

from typing import TYPE_CHECKING

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Club, Organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerFactory,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db

EVENT_CHANGELIST = "admin:events_event_changelist"
EVENT_ADD = "admin:events_event_add"
EVENT_CHANGE = "admin:events_event_change"


@pytest.fixture
def superuser_client(client: Client) -> Client:
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(account)
    return client


def _add_data(organizer: Organizer, **extra: str) -> dict[str, str]:
    data = {
        "name": "Made-up Fall Open",
        "kind": "tournament",
        "organizer": str(organizer.pk),
        "club": "",
        "start_date": "2026-09-05",
        "end_date": "2026-09-06",
    }
    data.update(extra)
    return data


def _form_errors(response: "_MonkeyPatchedWSGIResponse") -> dict[str, list[str]]:
    assert response.status_code == 200
    errors: dict[str, list[str]] = response.context["adminform"].form.errors
    return errors


def test_admin_settings() -> None:
    event_admin = admin.site.get_model_admin(Event)

    assert event_admin.list_display == (
        "public_code",
        "name",
        "kind",
        "organizer",
        "club",
        "start_date",
        "end_date",
    )
    assert "public_code" in event_admin.readonly_fields
    assert set(event_admin.search_fields) == {"public_code", "name"}
    assert event_admin.list_filter == ("kind",)
    assert set(event_admin.autocomplete_fields) == {"organizer", "club"}


def test_changelist_loads(superuser_client: Client) -> None:
    event = EventFactory.create()

    response = superuser_client.get(reverse(EVENT_CHANGELIST))

    assert response.status_code == 200
    assert event.public_code in response.content.decode()


def test_changelist_search_and_filter_load(superuser_client: Client) -> None:
    event = EventFactory.create()

    response = superuser_client.get(
        reverse(EVENT_CHANGELIST), {"q": event.public_code, "kind__exact": "league"}
    )

    assert response.status_code == 200
    assert event.public_code in response.content.decode()


def test_add_and_change_pages_load(superuser_client: Client) -> None:
    event = EventFactory.create()

    add = superuser_client.get(reverse(EVENT_ADD))
    change = superuser_client.get(reverse(EVENT_CHANGE, args=[event.pk]))

    assert add.status_code == 200
    assert change.status_code == 200
    assert event.public_code in change.content.decode()


def test_add_form_creates_an_event(superuser_client: Client) -> None:
    organizer: Organizer = OrganizerFactory.create()
    club: Club = ClubFactory.create()
    # ADR 0021: an Event names a Club only with the Club's grant.
    ClubOrganizerGrantFactory.create(club=club, organizer=organizer)

    response = superuser_client.post(
        reverse(EVENT_ADD), _add_data(organizer, club=str(club.pk))
    )

    assert response.status_code == 302
    event = Event.objects.get()
    assert event.public_code.startswith("E-")
    assert (event.organizer, event.club, event.kind) == (organizer, club, "tournament")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_date", "zqxjmarker"),
        ("end_date", "2026-02-31"),
    ],
)
def test_add_form_refuses_bad_values_without_echo(
    superuser_client: Client, field: str, value: str
) -> None:
    organizer: Organizer = OrganizerFactory.create()

    response = superuser_client.post(
        reverse(EVENT_ADD), _add_data(organizer, **{field: value})
    )

    errors = _form_errors(response)
    assert field in errors
    assert value not in " ".join(errors[field])
    assert not Event.objects.exists()


def test_add_form_refuses_end_date_before_start_date(
    superuser_client: Client,
) -> None:
    organizer: Organizer = OrganizerFactory.create()

    response = superuser_client.post(
        reverse(EVENT_ADD),
        _add_data(organizer, start_date="2026-09-06", end_date="2026-09-05"),
    )

    errors = _form_errors(response)
    assert "end_date" in errors
    assert not Event.objects.exists()


def test_organizer_delete_page_lists_the_event_as_protected(
    superuser_client: Client,
) -> None:
    event = EventFactory.create()
    url = reverse("admin:clubs_organizer_delete", args=[event.organizer.pk])

    page = superuser_client.get(url)
    posted = superuser_client.post(url, {"post": "yes"})

    assert page.status_code == 200
    assert "Cannot delete" in page.content.decode()
    assert event.public_code in page.content.decode()
    assert posted.status_code == 200
    assert Organizer.objects.filter(pk=event.organizer.pk).exists()


def test_club_delete_page_lists_the_event_as_protected(
    superuser_client: Client,
) -> None:
    club: Club = ClubFactory.create()
    event = EventFactory.create(club=club)
    url = reverse("admin:clubs_club_delete", args=[club.pk])

    page = superuser_client.get(url)
    posted = superuser_client.post(url, {"post": "yes"})

    assert page.status_code == 200
    assert "Cannot delete" in page.content.decode()
    assert event.public_code in page.content.decode()
    assert posted.status_code == 200
    assert Club.objects.filter(pk=club.pk).exists()

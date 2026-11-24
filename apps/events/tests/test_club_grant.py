"""An Event that names a Club needs that Club's grant to its Organizer.

ADR 0021: a Club decides who files under its name. ``Event.clean()``, and
so the admin form, refuses a new Event, or a change of its Club or
Organizer, when the Club has no Club Organizer Grant to the Event's
Organizer. An existing Event whose grant was removed later still saves an
unrelated change. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Club, ClubOrganizerGrant, Organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerFactory,
)
from apps.events.models import Event, EventKind

pytestmark = pytest.mark.django_db

REFUSED = "This club has not allowed the organizer to name it."


@pytest.fixture
def organizer() -> Organizer:
    made: Organizer = OrganizerFactory.create()
    return made


@pytest.fixture
def club(organizer: Organizer) -> Club:
    """A Club that granted ``organizer``."""
    made: Club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=made, organizer=organizer)
    return made


def _event(organizer: Organizer, **overrides: Any) -> Event:
    values: dict[str, Any] = {
        "name": "Made-up Spring League",
        "kind": EventKind.LEAGUE,
        "organizer": organizer,
        "start_date": date(2026, 3, 1),
        "end_date": date(2026, 5, 31),
    }
    values.update(overrides)
    return Event(**values)


def _club_errors(event: Event) -> list[str]:
    with pytest.raises(ValidationError) as error:
        event.full_clean()
    return error.value.message_dict.get("club", [])


def test_a_new_event_with_a_granting_club_passes(
    organizer: Organizer, club: Club
) -> None:
    _event(organizer, club=club).full_clean()


def test_a_new_event_with_no_club_passes(organizer: Organizer) -> None:
    _event(organizer).full_clean()


def test_a_new_event_with_a_club_that_did_not_grant_is_refused(
    organizer: Organizer,
) -> None:
    ungranted: Club = ClubFactory.create()

    assert _club_errors(_event(organizer, club=ungranted)) == [REFUSED]


def test_a_grant_to_another_organizer_is_not_enough(organizer: Organizer) -> None:
    other_club: Club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=other_club, organizer=OrganizerFactory())

    assert _club_errors(_event(organizer, club=other_club)) == [REFUSED]


def test_changing_the_club_to_one_that_did_not_grant_is_refused(
    organizer: Organizer, club: Club
) -> None:
    event = _event(organizer, club=club)
    event.full_clean()
    event.save()
    event.club = ClubFactory.create()

    assert _club_errors(event) == [REFUSED]


def test_changing_the_organizer_to_one_the_club_did_not_grant_is_refused(
    organizer: Organizer, club: Club
) -> None:
    event = _event(organizer, club=club)
    event.save()
    event.organizer = OrganizerFactory.create()

    assert _club_errors(event) == [REFUSED]


def test_an_unrelated_change_saves_after_the_grant_was_removed(
    organizer: Organizer, club: Club
) -> None:
    event = _event(organizer, club=club)
    event.save()
    ClubOrganizerGrant.objects.filter(club=club).delete()
    event.name = "Made-up Renamed League"

    event.full_clean()
    event.save()

    assert Event.objects.get(pk=event.pk).name == "Made-up Renamed League"


def test_the_stored_club_is_read_from_the_database(
    organizer: Organizer, club: Club
) -> None:
    # An Event saved with an ungranted Club (a raw write) and then cleaned
    # again keeps its stored Club, so it is not checked again; a change to a
    # granting Club passes.
    ungranted: Club = ClubFactory.create()
    event = _event(organizer, club=ungranted)
    event.save()

    event.full_clean()
    event.club = club
    event.full_clean()


@pytest.fixture
def superuser_client(client: Client) -> Client:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    return client


def _add_data(organizer: Organizer, club: Club) -> dict[str, str]:
    return {
        "name": "Made-up Fall Open",
        "kind": "tournament",
        "organizer": str(organizer.pk),
        "club": str(club.pk),
        "start_date": "2026-09-05",
        "end_date": "2026-09-06",
    }


def test_the_admin_add_form_refuses_a_club_that_did_not_grant(
    superuser_client: Client, organizer: Organizer
) -> None:
    ungranted: Club = ClubFactory.create()

    response = superuser_client.post(
        reverse("admin:events_event_add"), _add_data(organizer, ungranted)
    )

    assert response.status_code == 200
    errors = response.context["adminform"].form.errors
    assert errors["club"] == [REFUSED]
    assert str(ungranted.pk) not in REFUSED
    assert not Event.objects.exists()


def test_the_admin_add_form_accepts_a_granting_club(
    superuser_client: Client, organizer: Organizer, club: Club
) -> None:
    response = superuser_client.post(
        reverse("admin:events_event_add"), _add_data(organizer, club)
    )

    assert response.status_code == 302
    assert Event.objects.get().club == club

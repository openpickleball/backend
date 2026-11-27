"""A Club named on Events or recorded matches is not renamed by its admins
(#230).

Participants see a Club's name on trusted, recorded matches and Events,
so a Club admin's rename would change that text with no review, as a
verified Organizer's rename would (ADR 0054). While any Event or Match
names the Club, ``update_club`` refuses a change of ``name`` with
``Conflict`` (409); city and region may still change. Made-up data
only.
"""

import json
from collections.abc import Callable, Iterator

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.club_self_service import update_club
from apps.clubs.models import Club
from apps.clubs.self_service import Conflict
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    verified_organizer,
)
from apps.events.tests.factories import EventFactory
from apps.matches.tests.factories import MatchFactory

pytestmark = pytest.mark.django_db

RENAME_REFUSED = (
    "A club with events or recorded matches cannot be renamed here. "
    "Ask OPEN staff to rename it."
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def _admin_of(club: Club) -> User:
    admin: User = UserFactory.create()
    EmailAddress.objects.create(user=admin, email=admin.email, verified=True)
    enroll_totp(admin)
    ClubRoleFactory.create(club=club, account=admin)
    return admin


def _with_event() -> Club:
    club = ClubFactory.create(name="Made-up Named Courts")
    organizer = verified_organizer()
    ClubOrganizerGrantFactory.create(club=club, organizer=organizer)
    EventFactory.create(organizer=organizer, club=club)
    return club


def _with_match() -> Club:
    club = ClubFactory.create(name="Made-up Named Courts")
    MatchFactory.create(club=club)
    return club


@pytest.mark.parametrize("make", [_with_event, _with_match])
def test_a_rename_is_refused_while_the_club_is_named(
    make: Callable[[], Club],
) -> None:
    club = make()
    admin = _admin_of(club)

    with pytest.raises(Conflict) as caught:
        update_club(club.pk, actor=admin, name="Made-up New Name")

    assert str(caught.value) == RENAME_REFUSED
    club.refresh_from_db()
    assert club.name == "Made-up Named Courts"
    assert not AuditEvent.objects.filter(action=AuditAction.CLUB_CHANGE).exists()


def test_city_and_region_still_change_and_an_unchanged_name_passes() -> None:
    club = _with_event()
    admin = _admin_of(club)

    changed = update_club(
        club.pk,
        actor=admin,
        name="  Made-up Named Courts ",
        city="Made-up New Town",
        region="NR",
    )

    assert changed is True
    club.refresh_from_db()
    assert (club.name, club.city, club.region) == (
        "Made-up Named Courts",
        "Made-up New Town",
        "NR",
    )


def test_the_api_answers_409_with_fixed_text() -> None:
    club = _with_event()
    admin = _admin_of(club)
    client = Client()
    client.force_login(admin)

    response = client.patch(
        f"/api/v1/clubs/{club.public_code}",
        data=json.dumps({"name": "Made-up New Name"}),
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["message"] == RENAME_REFUSED

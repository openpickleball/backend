"""An explicit null in a Club PATCH means "not changed" (#230).

The OpenAPI document marks ``name``, ``city``, and ``region`` on
``ClubUpdateIn`` as nullable, so a null must be accepted, and it changes
nothing, the same as a missing key. Made-up data only.
"""

import json
from collections.abc import Iterator

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import ClubFactory, ClubRoleFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.mark.parametrize(
    "body",
    [
        {"name": None},
        {"city": None},
        {"region": None},
        {"name": None, "city": None, "region": None},
    ],
)
def test_a_null_field_is_not_changed(body: dict[str, None]) -> None:
    club = ClubFactory.create(name="Made-up Kept", city="Made-up Town", region="MT")
    admin: User = UserFactory.create()
    EmailAddress.objects.create(user=admin, email=admin.email, verified=True)
    enroll_totp(admin)
    ClubRoleFactory.create(club=club, account=admin)
    client = Client()
    client.force_login(admin)

    response = client.patch(
        f"/api/v1/clubs/{club.public_code}",
        data=json.dumps(body),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json() == {
        "club": club.public_code,
        "name": "Made-up Kept",
        "city": "Made-up Town",
        "region": "MT",
    }
    assert not AuditEvent.objects.filter(action=AuditAction.CLUB_CHANGE).exists()


def test_a_null_city_with_a_new_region_changes_only_the_region() -> None:
    club = ClubFactory.create(name="Made-up Kept", city="Made-up Town", region="MT")
    admin: User = UserFactory.create()
    EmailAddress.objects.create(user=admin, email=admin.email, verified=True)
    enroll_totp(admin)
    ClubRoleFactory.create(club=club, account=admin)
    client = Client()
    client.force_login(admin)

    response = client.patch(
        f"/api/v1/clubs/{club.public_code}",
        data=json.dumps({"city": None, "region": "NR"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert (response.json()["city"], response.json()["region"]) == (
        "Made-up Town",
        "NR",
    )

"""The member and Club role inlines refuse a tampered role without echo.

Issue #162. Django's form-level ``invalid_choice`` message names the
submitted value, and the model field's ``error_messages`` do not reach
the admin's inline forms. Made-up data only.
"""

from typing import TYPE_CHECKING

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import ClubRole, OrganizerMember
from apps.clubs.tests.factories import ClubFactory, OrganizerFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

BAD_ROLES = ["987654321", "zqxjmarker"]


@pytest.fixture
def admin_client(client: Client) -> Client:
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(account)
    return client


def _formset(prefix: str, row: dict[str, str]) -> dict[str, str]:
    data = {
        f"{prefix}-TOTAL_FORMS": "1",
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for key, value in row.items():
        data[f"{prefix}-0-{key}"] = value
    return data


def _row_errors(response: object) -> str:
    formsets = response.context["inline_admin_formsets"]  # type: ignore[attr-defined]
    return " ".join(
        " ".join(" ".join(items) for items in form.errors.values())
        for inline in formsets
        for form in inline.formset.forms
    )


@pytest.mark.parametrize("role", BAD_ROLES)
def test_member_inline_refuses_tampered_role_without_echo(
    admin_client: Client, role: str
) -> None:
    organizer = OrganizerFactory.create()
    account = UserFactory.create()
    data = {
        "name": organizer.name,
        **_formset(
            "members",
            {
                "id": "",
                "organizer": str(organizer.pk),
                "account": str(account.pk),
                "role": role,
            },
        ),
    }

    response = admin_client.post(
        reverse("admin:clubs_organizer_change", args=[organizer.pk]), data
    )

    assert response.status_code == 200
    errors = _row_errors(response)
    assert "Choose one of the listed options." in errors
    assert role not in errors
    assert role not in response.content.decode()
    assert not OrganizerMember.objects.exists()


@pytest.mark.parametrize("role", BAD_ROLES)
def test_club_role_inline_refuses_tampered_role_without_echo(
    admin_client: Client, role: str
) -> None:
    club = ClubFactory.create()
    account = UserFactory.create()
    data = {
        "name": club.name,
        "city": club.city,
        "region": club.region,
        **_formset(
            "roles",
            {
                "id": "",
                "club": str(club.pk),
                "account": str(account.pk),
                "role": role,
            },
        ),
    }

    response = admin_client.post(
        reverse("admin:clubs_club_change", args=[club.pk]), data
    )

    assert response.status_code == 200
    errors = _row_errors(response)
    assert "Choose one of the listed options." in errors
    assert role not in errors
    assert role not in response.content.decode()
    assert not ClubRole.objects.exists()

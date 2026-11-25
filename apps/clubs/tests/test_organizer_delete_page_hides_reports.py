"""The Organizer delete pages never list its reports by row (#271).

A report on an Organizer blocks its delete (``PROTECT``). Django's delete
confirmation lists related rows by text with a link, which would bypass
the report admin's rule that a member never reaches its own
Organizer's reports. Both the delete view and ``delete_selected`` show
only the type. Made-up data only.
"""

import re

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Organizer, OrganizerReport, OrganizerReportReason
from apps.clubs.reports import report_organizer
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _verifier() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(Permission.objects.get(codename="verify_organizer"))
    return account


@pytest.fixture
def setup() -> tuple[Client, Organizer, list[OrganizerReport]]:
    """A staff member of an unverified Organizer that has closed reports."""
    organizer = verified_organizer()
    reports = [
        report_organizer(
            organizer.pk,
            actor=PlayerFactory.create().account,
            reason=OrganizerReportReason.OTHER,
        )
        for _ in range(2)
    ]
    unverify_organizer(organizer, actor=_verifier(), reason="Made-up reason.")
    staff: User = UserFactory.create(is_staff=True)
    staff.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="clubs",
            codename__in=["view_organizer", "delete_organizer"],
        )
    )
    OrganizerMemberFactory.create(organizer=organizer, account=staff)
    client = Client()
    client.force_login(staff)
    return client, organizer, reports


def _assert_no_report_rows(page: str, reports: list[OrganizerReport]) -> None:
    for report in reports:
        assert not re.search(rf"Organizer report {report.pk}\b", page)
    assert "/admin/clubs/organizerreport/" not in page
    assert "organizer report" in page


def test_the_delete_view_lists_no_report_rows(
    setup: tuple[Client, Organizer, list[OrganizerReport]],
) -> None:
    client, organizer, reports = setup

    response = client.get(reverse("admin:clubs_organizer_delete", args=[organizer.pk]))

    assert response.status_code == 200
    _assert_no_report_rows(response.content.decode(), reports)
    assert Organizer.objects.filter(pk=organizer.pk).exists()


def test_delete_selected_lists_no_report_rows(
    setup: tuple[Client, Organizer, list[OrganizerReport]],
) -> None:
    client, organizer, reports = setup

    response = client.post(
        reverse("admin:clubs_organizer_changelist"),
        {"action": "delete_selected", "_selected_action": [str(organizer.pk)]},
    )

    assert response.status_code == 200
    _assert_no_report_rows(response.content.decode(), reports)
    assert Organizer.objects.filter(pk=organizer.pk).exists()

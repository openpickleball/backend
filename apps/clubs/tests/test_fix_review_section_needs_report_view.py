"""The moderator review needs the report view permission too (#271).

The review links to the report admin, which needs
``clubs.view_organizerreport``. An Account that may verify but not view
reports would get links that answer 403, so it sees no review. Made-up
data only.
"""

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import OrganizerReportReason
from apps.clubs.reports import report_organizer
from apps.clubs.tests.factories import verified_organizer
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REVIEW_HEADING = "Moderator review"


def _staff(*codenames: str) -> User:
    account: User = UserFactory.create(is_staff=True)
    account.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="clubs", codename__in=codenames
        )
    )
    return account


def _page(account: User) -> str:
    organizer = verified_organizer()
    report_organizer(
        organizer.pk,
        actor=PlayerFactory.create().account,
        reason=OrganizerReportReason.OTHER,
    )
    client = Client()
    client.force_login(account)
    response = client.get(reverse("admin:clubs_organizer_change", args=[organizer.pk]))
    assert response.status_code == 200
    return response.content.decode()


def test_verify_without_report_view_shows_no_review() -> None:
    page = _page(_staff("view_organizer", "verify_organizer"))

    assert REVIEW_HEADING not in page


def test_verify_with_report_view_shows_the_review() -> None:
    page = _page(_staff("view_organizer", "verify_organizer", "view_organizerreport"))

    assert REVIEW_HEADING in page

"""The report admin refuses any changelist lookup on the reporter (#271).

Filtering by ``reporter`` would let a viewer find which reports one
Account made, by its row id. Django answers a refused lookup with 400.
Made-up data only.
"""

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import OrganizerReportReason
from apps.clubs.reports import report_organizer
from apps.clubs.tests.factories import verified_organizer
from apps.matches.permissions import MODERATOR_GROUP
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _moderator_client() -> Client:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    client = Client()
    client.force_login(account)
    return client


@pytest.mark.parametrize(
    "lookup",
    [
        "reporter",
        "reporter__exact",
        "reporter_id",
        "reporter__id",
        "reporter__isnull",
        "reporter__in",
    ],
)
def test_a_reporter_lookup_is_refused(lookup: str) -> None:
    player: Player = PlayerFactory.create()
    report_organizer(
        verified_organizer().pk,
        actor=player.account,
        reason=OrganizerReportReason.OTHER,
    )
    value = "True" if lookup.endswith("isnull") else str(player.account.pk)

    response = _moderator_client().get(
        reverse("admin:clubs_organizerreport_changelist"), {lookup: value}
    )

    assert response.status_code == 400


def test_the_listed_filters_still_work() -> None:
    response = _moderator_client().get(
        reverse("admin:clubs_organizerreport_changelist"),
        {"reason__exact": "other", "open": "yes"},
    )

    assert response.status_code == 200

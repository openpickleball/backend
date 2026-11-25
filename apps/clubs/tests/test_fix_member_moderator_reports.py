"""A moderator who is a member of an Organizer never sees its reports (#271).

A staff Account with ``clubs.verify_organizer`` that is also an
Organizer Member of X must not learn who reported X: X's reports are left
out of the report admin (changelist, change page, close action), and X's
change page shows no moderator review. A moderator who is not a member
still sees everything. Made-up data only.
"""

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Organizer, OrganizerReport, OrganizerReportReason
from apps.clubs.reports import report_organizer
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.matches.permissions import MODERATOR_GROUP
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REVIEW_HEADING = "Moderator review"
CHANGELIST = "admin:clubs_organizerreport_changelist"


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _report(organizer: Organizer) -> tuple[OrganizerReport, Player]:
    player: Player = PlayerFactory.create()
    report = report_organizer(
        organizer.pk, actor=player.account, reason=OrganizerReportReason.OTHER
    )
    return report, player


@pytest.fixture
def member_moderator() -> tuple[User, Organizer, Organizer]:
    """A moderator who is a member of ``own``, and another Organizer."""
    own = verified_organizer()
    other = verified_organizer()
    moderator = _moderator()
    OrganizerMemberFactory.create(organizer=own, account=moderator)
    return moderator, own, other


def test_a_member_moderator_gets_no_review_on_its_organizer(
    member_moderator: tuple[User, Organizer, Organizer],
) -> None:
    moderator, own, other = member_moderator
    _report(own)
    _report(other)
    client = _client(moderator)

    own_page = client.get(reverse("admin:clubs_organizer_change", args=[own.pk]))
    other_page = client.get(reverse("admin:clubs_organizer_change", args=[other.pk]))

    assert own_page.status_code == 200
    assert REVIEW_HEADING not in own_page.content.decode()
    assert REVIEW_HEADING in other_page.content.decode()


def test_a_member_moderator_does_not_list_its_organizers_reports(
    member_moderator: tuple[User, Organizer, Organizer],
) -> None:
    moderator, own, other = member_moderator
    _report(own)
    other_report, _player = _report(other)

    response = _client(moderator).get(reverse(CHANGELIST))

    assert [r.pk for r in response.context["cl"].result_list] == [other_report.pk]


def test_a_member_moderator_cannot_open_its_organizers_report(
    member_moderator: tuple[User, Organizer, Organizer],
) -> None:
    moderator, own, _other = member_moderator
    report, player = _report(own)

    response = _client(moderator).get(
        reverse("admin:clubs_organizerreport_change", args=[report.pk])
    )

    assert response.status_code in (302, 404)
    assert player.public_code not in response.content.decode()


def test_a_member_moderator_close_action_never_reaches_the_report(
    member_moderator: tuple[User, Organizer, Organizer],
) -> None:
    moderator, own, _other = member_moderator
    report, _player = _report(own)

    _client(moderator).post(
        reverse(CHANGELIST),
        {
            "action": "close_selected",
            "_selected_action": [str(report.pk)],
            "reason": "Made-up reason.",
        },
    )

    report.refresh_from_db()
    assert report.closed_at is None


def test_a_non_member_moderator_still_sees_every_report(
    member_moderator: tuple[User, Organizer, Organizer],
) -> None:
    _moderator_member, own, other = member_moderator
    own_report, player = _report(own)
    other_report, _ = _report(other)
    client = _client(_moderator())

    listing = client.get(reverse(CHANGELIST))
    page = client.get(
        reverse("admin:clubs_organizerreport_change", args=[own_report.pk])
    )

    assert {r.pk for r in listing.context["cl"].result_list} == {
        own_report.pk,
        other_report.pk,
    }
    assert player.public_code in page.content.decode()
    assert (
        REVIEW_HEADING
        in client.get(
            reverse("admin:clubs_organizer_change", args=[own.pk])
        ).content.decode()
    )

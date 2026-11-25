"""The Organizer Report model (#271).

One open report per Account and Organizer, enforced by the database. A
report's text never names the Organizer or the reporter. The Moderator
group may view reports. Made-up data only.
"""

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import OrganizerReport, OrganizerReportReason
from apps.clubs.tests.factories import OrganizerFactory
from apps.matches.permissions import MODERATOR_GROUP, MODERATOR_PERMISSIONS

pytestmark = pytest.mark.django_db


def test_the_reasons_are_the_five_decided() -> None:
    assert set(OrganizerReportReason.values) == {
        "wrong_results",
        "fake_matches",
        "ignores_disputes",
        "misuses_player_data",
        "other",
    }


def test_two_open_reports_by_one_account_are_refused_by_the_database() -> None:
    organizer = OrganizerFactory.create()
    account = UserFactory.create()
    OrganizerReport.objects.create(
        organizer=organizer, reporter=account, reason=OrganizerReportReason.OTHER
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizerReport.objects.create(
            organizer=organizer,
            reporter=account,
            reason=OrganizerReportReason.FAKE_MATCHES,
        )


def test_a_closed_report_allows_a_new_open_one() -> None:
    organizer = OrganizerFactory.create()
    account = UserFactory.create()
    OrganizerReport.objects.create(
        organizer=organizer,
        reporter=account,
        reason=OrganizerReportReason.OTHER,
        closed_at=timezone.now(),
    )

    OrganizerReport.objects.create(
        organizer=organizer, reporter=account, reason=OrganizerReportReason.OTHER
    )

    assert OrganizerReport.objects.count() == 2


def test_an_unknown_reason_is_refused_by_the_database() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizerReport.objects.create(
            organizer=OrganizerFactory.create(),
            reporter=UserFactory.create(),
            reason="made_up_reason",
        )


def test_an_unknown_reason_message_does_not_echo_it() -> None:
    report = OrganizerReport(
        organizer=OrganizerFactory.create(),
        reporter=UserFactory.create(),
        reason="made-up-MARKER",
    )

    with pytest.raises(ValidationError) as caught:
        report.full_clean()

    assert "MARKER" not in str(caught.value)


def test_str_names_only_the_organizer_code() -> None:
    organizer = OrganizerFactory.create(name="Made-up Person Name")
    account = UserFactory.create()
    report = OrganizerReport.objects.create(
        organizer=organizer, reporter=account, reason=OrganizerReportReason.OTHER
    )

    text = str(report)

    assert organizer.public_code in text
    assert "Made-up Person Name" not in text
    assert account.email not in text


def test_the_moderator_group_may_view_reports() -> None:
    group = Group.objects.get(name=MODERATOR_GROUP)

    codes = {
        f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()
    }

    assert "clubs.view_organizerreport" in codes
    assert "clubs.view_organizerreport" in MODERATOR_PERMISSIONS

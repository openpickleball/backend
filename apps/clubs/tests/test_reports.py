"""Reporting an Organizer and closing reports (#271).

Any active Account with a Player may report a verified Organizer, once
while the report is open. A moderator closes a report with a reason, and
unverifying the Organizer closes all its open reports. Each create and
close records one Audit Event. Nothing else changes. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import (
    Organizer,
    OrganizerReport,
    OrganizerReportReason,
)
from apps.clubs.reports import (
    NotAllowedToClose,
    ReportExists,
    ReportRefused,
    close_report,
    report_organizer,
)
from apps.clubs.services import unverify_organizer, verify_organizer
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFlag, MatchFormat
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import RatingHistory, RatingState

pytestmark = pytest.mark.django_db

REASON = "Made-up moderator reason."
ORGANIZER_NAME = "Made-up Organizer Person"


def _reporter() -> User:
    account: User = PlayerFactory.create().account
    return account


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _verifier() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    return account


def _report(organizer: Organizer, account: User | None = None) -> OrganizerReport:
    return report_organizer(
        organizer.pk,
        actor=account or _reporter(),
        reason=OrganizerReportReason.WRONG_RESULTS,
    )


def _events(action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action).order_by("pk"))


# Reporting


def test_a_player_reports_a_verified_organizer() -> None:
    organizer = verified_organizer(name=ORGANIZER_NAME)
    account = _reporter()

    report = report_organizer(
        organizer.pk, actor=account, reason=OrganizerReportReason.FAKE_MATCHES
    )

    stored = OrganizerReport.objects.get()
    assert stored.pk == report.pk
    assert stored.organizer_id == organizer.pk
    assert stored.reporter_id == account.pk
    assert stored.reason == OrganizerReportReason.FAKE_MATCHES
    assert stored.closed_at is None


def test_a_report_records_one_audit_event() -> None:
    organizer = verified_organizer(name=ORGANIZER_NAME)
    account = _reporter()

    report = _report(organizer, account)

    (event,) = _events(AuditAction.ORGANIZER_REPORT_CREATE)
    assert event.actor_user_id == account.pk
    assert event.target_content_type == ContentType.objects.get_for_model(
        OrganizerReport
    )
    assert event.target_object_id == str(report.pk)
    assert event.after == {
        "reason": "wrong_results",
        "organizer": organizer.public_code,
    }
    assert event.reason == ""
    stored = str(event.before) + str(event.after) + event.reason
    assert ORGANIZER_NAME not in stored
    assert account.email not in stored


@pytest.mark.parametrize("reason", ["", "made_up", None, 5])
def test_an_unknown_reason_is_refused(reason: Any) -> None:
    organizer = verified_organizer()

    with pytest.raises(ValueError, match="listed reasons"):
        report_organizer(organizer.pk, actor=_reporter(), reason=reason)

    assert not OrganizerReport.objects.exists()


def test_an_unverified_organizer_is_refused() -> None:
    organizer = OrganizerFactory.create()

    with pytest.raises(ReportRefused):
        _report(organizer)

    assert not OrganizerReport.objects.exists()
    assert not _events(AuditAction.ORGANIZER_REPORT_CREATE)


def test_an_unknown_organizer_is_refused() -> None:
    with pytest.raises(ReportRefused):
        report_organizer(
            987654321, actor=_reporter(), reason=OrganizerReportReason.OTHER
        )


def test_an_account_with_no_player_is_refused() -> None:
    organizer = verified_organizer()

    with pytest.raises(ReportRefused):
        _report(organizer, UserFactory.create())

    assert not OrganizerReport.objects.exists()


def test_an_inactive_account_is_refused() -> None:
    organizer = verified_organizer()
    account = _reporter()
    account.is_active = False
    account.save()

    with pytest.raises(ReportRefused):
        _report(organizer, account)

    assert not OrganizerReport.objects.exists()


def test_a_stale_active_instance_is_refused() -> None:
    """The service reads ``is_active`` from the database, not the instance."""
    organizer = verified_organizer()
    account = _reporter()
    User.objects.filter(pk=account.pk).update(is_active=False)

    with pytest.raises(ReportRefused):
        _report(organizer, account)


def test_refusals_share_one_message() -> None:
    messages = set()
    for organizer_id, account in (
        (OrganizerFactory.create().pk, _reporter()),
        (987654321, _reporter()),
        (verified_organizer().pk, UserFactory.create()),
    ):
        with pytest.raises(ReportRefused) as caught:
            report_organizer(
                organizer_id, actor=account, reason=OrganizerReportReason.OTHER
            )
        messages.add(str(caught.value))

    assert len(messages) == 1


def test_a_second_open_report_is_refused() -> None:
    organizer = verified_organizer()
    account = _reporter()
    _report(organizer, account)

    with pytest.raises(ReportExists):
        _report(organizer, account)

    assert OrganizerReport.objects.count() == 1
    assert len(_events(AuditAction.ORGANIZER_REPORT_CREATE)) == 1


def test_another_account_may_report_the_same_organizer() -> None:
    organizer = verified_organizer()
    _report(organizer)
    _report(organizer)

    assert OrganizerReport.objects.count() == 2


def test_after_a_close_the_account_may_report_again() -> None:
    organizer = verified_organizer()
    account = _reporter()
    first = _report(organizer, account)
    close_report(first, actor=_moderator(), reason=REASON)

    _report(organizer, account)

    assert OrganizerReport.objects.filter(closed_at__isnull=True).count() == 1


# Closing by a moderator


def test_a_moderator_closes_a_report() -> None:
    organizer = verified_organizer()
    report = _report(organizer)
    moderator = _moderator()

    assert close_report(report, actor=moderator, reason=REASON) is True

    report.refresh_from_db()
    assert report.closed_at is not None
    (event,) = _events(AuditAction.ORGANIZER_REPORT_CLOSE)
    assert event.actor_user_id == moderator.pk
    assert event.target_object_id == str(report.pk)
    assert event.after is not None
    assert event.after["rule"] == "moderator"
    assert event.reason == REASON


def test_closing_a_closed_report_changes_nothing() -> None:
    report = _report(verified_organizer())
    moderator = _moderator()
    close_report(report, actor=moderator, reason=REASON)
    closed_at = OrganizerReport.objects.get().closed_at

    assert close_report(report, actor=moderator, reason=REASON) is False

    assert OrganizerReport.objects.get().closed_at == closed_at
    assert len(_events(AuditAction.ORGANIZER_REPORT_CLOSE)) == 1


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_close_needs_a_reason(reason: str) -> None:
    report = _report(verified_organizer())

    with pytest.raises(ValueError, match="reason"):
        close_report(report, actor=_moderator(), reason=reason)

    assert OrganizerReport.objects.get().closed_at is None
    assert not _events(AuditAction.ORGANIZER_REPORT_CLOSE)


def test_an_account_without_permission_may_not_close() -> None:
    report = _report(verified_organizer())

    with pytest.raises(NotAllowedToClose):
        close_report(report, actor=UserFactory.create(is_staff=True), reason=REASON)

    assert OrganizerReport.objects.get().closed_at is None


def test_an_inactive_moderator_may_not_close() -> None:
    report = _report(verified_organizer())
    moderator = _moderator()
    moderator.is_active = False
    moderator.save()

    with pytest.raises(NotAllowedToClose):
        close_report(report, actor=moderator, reason=REASON)


def test_a_member_of_the_organizer_may_not_close() -> None:
    organizer = verified_organizer()
    report = _report(organizer)
    moderator = _moderator()
    OrganizerMemberFactory.create(organizer=organizer, account=moderator)

    with pytest.raises(NotAllowedToClose):
        close_report(report, actor=moderator, reason=REASON)

    assert OrganizerReport.objects.get().closed_at is None
    assert not _events(AuditAction.ORGANIZER_REPORT_CLOSE)


# Closing by unverifying the Organizer


def test_unverifying_closes_every_open_report() -> None:
    organizer = verified_organizer()
    first = _report(organizer)
    second = _report(organizer)
    other = _report(verified_organizer())
    verifier = _verifier()

    unverify_organizer(organizer, actor=verifier, reason=REASON)

    assert OrganizerReport.objects.filter(closed_at__isnull=True).get() == other
    events = _events(AuditAction.ORGANIZER_REPORT_CLOSE)
    assert sorted(int(e.target_object_id) for e in events) == sorted(
        [first.pk, second.pk]
    )
    for event in events:
        assert event.actor_user_id == verifier.pk
        assert event.reason == REASON
        assert event.after is not None
        assert event.after["rule"] == "organizer_unverified"


def test_unverifying_leaves_closed_reports_alone() -> None:
    organizer = verified_organizer()
    report = _report(organizer)
    close_report(report, actor=_moderator(), reason=REASON)

    unverify_organizer(organizer, actor=_verifier(), reason=REASON)

    assert len(_events(AuditAction.ORGANIZER_REPORT_CLOSE)) == 1


def test_a_noop_unverify_closes_nothing() -> None:
    # Only a raw write leaves an open report on an unverified Organizer.
    organizer = OrganizerFactory.create()
    report = OrganizerReport.objects.create(
        organizer=organizer,
        reporter=_reporter(),
        reason=OrganizerReportReason.OTHER,
    )

    unverify_organizer(organizer, actor=_verifier(), reason=REASON)

    report.refresh_from_db()
    assert report.closed_at is None
    assert not _events(AuditAction.ORGANIZER_REPORT_CLOSE)


def test_verifying_closes_nothing() -> None:
    organizer = verified_organizer()
    report = _report(organizer)

    verify_organizer(organizer, actor=_verifier(), reason=REASON)

    report.refresh_from_db()
    assert report.closed_at is None
    assert not _events(AuditAction.ORGANIZER_REPORT_CLOSE)


# Nothing else changes


def _ledger_state() -> dict[str, Any]:
    return {
        "matches": list(Match.objects.order_by("pk").values()),
        "flags": list(MatchFlag.objects.order_by("pk").values()),
        "states": list(RatingState.objects.order_by("pk").values()),
        "history": list(RatingHistory.objects.order_by("pk").values()),
        "organizers": list(Organizer.objects.order_by("pk").values()),
    }


def test_reporting_and_closing_change_nothing_else() -> None:
    organizer = verified_organizer()
    member = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=member)
    players = [PlayerFactory.create() for _ in range(4)]
    record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=date(2026, 6, 1),
        submitted_by_user=member,
        event=EventFactory.create(organizer=organizer),
    )
    before = _ledger_state()

    report = _report(organizer, players[0].account)
    close_report(report, actor=_moderator(), reason=REASON)
    _report(organizer, players[1].account)

    assert _ledger_state() == before

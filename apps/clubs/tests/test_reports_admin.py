"""The admin for Organizer Reports and the Organizer's moderator review (#271).

A moderator closes open reports from the report admin with a typed
reason. The report admin has no add, change, or delete, and shows the
reporter only as their Player's OPEN Code, only to moderators. The
Organizer change page shows moderators a read-only review section.
Messages and pages never hold a name or an email. Made-up data only.
"""

from datetime import date

import pytest
from django.contrib.admin import site
from django.contrib.auth.models import Group, Permission
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import Organizer, OrganizerReport, OrganizerReportReason
from apps.clubs.reports import report_organizer
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches.models import MatchFormat
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.matches.workflow import dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up moderator reason."
ORGANIZER_NAME = "Made-up Organizer Person"
CHANGELIST = "admin:clubs_organizerreport_changelist"
BLANK_WARNING = "Give a reason to close reports."
REVIEW_HEADING = "Moderator review"


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _staff_viewer() -> User:
    """Staff who may view organizers and reports, but not verify."""
    account: User = UserFactory.create(is_staff=True)
    account.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="clubs",
            codename__in=["view_organizer", "view_organizerreport"],
        )
    )
    return account


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _report(
    organizer: Organizer, reason: str = OrganizerReportReason.FAKE_MATCHES
) -> OrganizerReport:
    player: Player = PlayerFactory.create()
    return report_organizer(organizer.pk, actor=player.account, reason=reason)


def _close(
    client: Client, reports: list[OrganizerReport], reason: str | None
) -> list[str]:
    data: dict[str, object] = {
        "action": "close_selected",
        "_selected_action": [str(r.pk) for r in reports],
    }
    if reason is not None:
        data["reason"] = reason
    response = client.post(reverse(CHANGELIST), data, follow=True)
    assert response.status_code == 200
    return [str(m) for m in get_messages(response.wsgi_request)]


def _organizer_page(client: Client, organizer: Organizer) -> str:
    response = client.get(reverse("admin:clubs_organizer_change", args=[organizer.pk]))
    assert response.status_code == 200
    return response.content.decode()


# Closing reports


def test_a_moderator_closes_reports_with_a_reason() -> None:
    organizer = verified_organizer()
    reports = [_report(organizer), _report(organizer)]
    moderator = _moderator()

    messages = _close(_client(moderator), reports, REASON)

    assert messages == ["2 report(s) closed."]
    assert not OrganizerReport.objects.filter(closed_at__isnull=True).exists()
    events = AuditEvent.objects.filter(action=AuditAction.ORGANIZER_REPORT_CLOSE)
    assert events.count() == 2
    assert {e.reason for e in events} == {REASON}
    assert {e.actor_user_id for e in events} == {moderator.pk}


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_a_blank_reason_changes_nothing(reason: str | None) -> None:
    report = _report(verified_organizer())

    messages = _close(_client(_moderator()), [report], reason)

    assert messages == [BLANK_WARNING]
    assert OrganizerReport.objects.get().closed_at is None
    assert not AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_REPORT_CLOSE
    ).exists()


def test_an_over_long_reason_changes_nothing() -> None:
    report = _report(verified_organizer())

    messages = _close(_client(_moderator()), [report], "x" * 1001)

    assert messages == ["Keep the reason to 1000 characters or fewer."]
    assert OrganizerReport.objects.get().closed_at is None


def test_an_already_closed_report_changes_nothing() -> None:
    report = _report(verified_organizer())
    client = _client(_moderator())
    _close(client, [report], REASON)

    messages = _close(client, [report], REASON)

    assert messages == []
    assert (
        AuditEvent.objects.filter(action=AuditAction.ORGANIZER_REPORT_CLOSE).count()
        == 1
    )


def test_a_moderator_who_became_a_member_gets_a_warning_naming_the_code() -> None:
    """The admin's list leaves out a member's Organizers' reports. One
    selected before the membership began still meets the service's check.
    """
    organizer = verified_organizer(name=ORGANIZER_NAME)
    report = _report(organizer)
    moderator = _moderator()
    OrganizerMemberFactory.create(organizer=organizer, account=moderator)
    request = RequestFactory().post(reverse(CHANGELIST), {"reason": REASON})
    request.user = moderator
    request.session = {}  # type: ignore[assignment]
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    model_admin = site._registry[OrganizerReport]

    model_admin.close_selected(  # type: ignore[attr-defined]
        request, OrganizerReport.objects.filter(pk=report.pk)
    )

    messages = [str(m) for m in get_messages(request)]
    assert messages == [
        f"Report on {organizer.public_code} was not closed: you may not close it."
    ]
    assert OrganizerReport.objects.get().closed_at is None
    assert ORGANIZER_NAME not in " ".join(messages)


def test_staff_without_permission_cannot_close() -> None:
    report = _report(verified_organizer())
    client = _client(_staff_viewer())

    page = client.get(reverse(CHANGELIST)).content.decode()
    _close(client, [report], REASON)

    assert "close_selected" not in page
    assert OrganizerReport.objects.get().closed_at is None
    assert not AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_REPORT_CLOSE
    ).exists()


# What the report admin shows


def test_the_report_admin_has_no_add_change_or_delete() -> None:
    report = _report(verified_organizer())
    client = _client(_moderator())
    change = reverse("admin:clubs_organizerreport_change", args=[report.pk])

    assert client.get(reverse("admin:clubs_organizerreport_add")).status_code == 403
    assert (
        client.get(
            reverse("admin:clubs_organizerreport_delete", args=[report.pk])
        ).status_code
        == 403
    )
    page = client.get(change)
    assert page.status_code == 200
    client.post(change, {"reason": "other"})
    report.refresh_from_db()
    assert report.reason == OrganizerReportReason.FAKE_MATCHES
    assert "delete_selected" not in client.get(reverse(CHANGELIST)).content.decode()


def test_a_moderator_sees_the_reporter_as_a_player_code_only() -> None:
    organizer = verified_organizer(name=ORGANIZER_NAME)
    player: Player = PlayerFactory.create()
    report = report_organizer(
        organizer.pk, actor=player.account, reason=OrganizerReportReason.OTHER
    )
    client = _client(_moderator())

    page = client.get(
        reverse("admin:clubs_organizerreport_change", args=[report.pk])
    ).content.decode()
    listing = client.get(reverse(CHANGELIST)).content.decode()

    assert player.public_code in page
    assert organizer.public_code in page
    for text in (page, listing):
        assert player.account.email not in text
        assert player.display_name not in text
        assert ORGANIZER_NAME not in text


def test_staff_without_verify_never_see_the_reporter() -> None:
    player: Player = PlayerFactory.create()
    report = report_organizer(
        verified_organizer().pk,
        actor=player.account,
        reason=OrganizerReportReason.OTHER,
    )

    page = (
        _client(_staff_viewer())
        .get(reverse("admin:clubs_organizerreport_change", args=[report.pk]))
        .content.decode()
    )

    assert player.public_code not in page
    assert player.account.email not in page


def test_the_list_filters_open_reports() -> None:
    organizer = verified_organizer()
    closed = _report(organizer)
    client = _client(_moderator())
    _close(client, [closed], REASON)
    still_open = _report(organizer)

    response = client.get(reverse(CHANGELIST), {"open": "yes"})

    shown = [r.pk for r in response.context["cl"].result_list]
    assert shown == [still_open.pk]


# The Organizer's moderator review


def test_a_moderator_sees_the_review_section() -> None:
    organizer = verified_organizer(name=ORGANIZER_NAME)
    report = _report(organizer, OrganizerReportReason.IGNORES_DISPUTES)
    players = [PlayerFactory.create() for _ in range(4)]
    member = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=member)
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=date(2026, 6, 1),
        submitted_by_user=member,
        event=EventFactory.create(organizer=organizer),
    ).match
    dispute_match(match, actor=players[0].account)

    page = _organizer_page(_client(_moderator()), organizer)

    assert REVIEW_HEADING in page
    assert "Ignores disputes" in page
    assert reverse("admin:clubs_organizerreport_change", args=[report.pk]) in page
    assert "Disputes opened: 1" in page
    assert "last 90 days" in page
    for player in players:
        assert player.account.email not in page
    assert report.reporter.email not in page


def test_staff_without_verify_see_no_review_section() -> None:
    organizer = verified_organizer()
    _report(organizer)

    page = _organizer_page(_client(_staff_viewer()), organizer)

    assert REVIEW_HEADING not in page
    assert "Disputes opened" not in page


def test_the_add_page_has_no_review_section() -> None:
    page = _client(_moderator()).get(reverse("admin:clubs_organizer_add"))

    assert page.status_code in (200, 403)
    assert REVIEW_HEADING not in page.content.decode()


def test_unverifying_from_the_admin_closes_open_reports() -> None:
    organizer = verified_organizer()
    report = _report(organizer)
    moderator = _moderator()

    _client(moderator).post(
        reverse("admin:clubs_organizer_changelist"),
        {
            "action": "unverify_selected",
            "_selected_action": [str(organizer.pk)],
            "reason": REASON,
        },
    )

    report.refresh_from_db()
    assert report.closed_at is not None
    event = AuditEvent.objects.get(action=AuditAction.ORGANIZER_REPORT_CLOSE)
    assert event.actor_user_id == moderator.pk
    assert event.after is not None
    assert event.after["rule"] == "organizer_unverified"


def test_a_report_removed_mid_action_gets_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organizer = verified_organizer()
    report = _report(organizer)

    def removed(*args: object, **kwargs: object) -> bool:
        raise OrganizerReport.DoesNotExist

    monkeypatch.setattr("apps.clubs.admin.close_report", removed)

    messages = _close(_client(_moderator()), [report], REASON)

    assert messages == [
        f"Report on {organizer.public_code} was not closed: it no longer exists."
    ]


def test_the_list_filters_closed_reports() -> None:
    organizer = verified_organizer()
    closed = _report(organizer)
    client = _client(_moderator())
    _close(client, [closed], REASON)
    _report(organizer)

    response = client.get(reverse(CHANGELIST), {"open": "no"})

    shown = [r.pk for r in response.context["cl"].result_list]
    assert shown == [closed.pk]


def test_a_member_moderator_mixed_selection_closes_only_the_other() -> None:
    own = verified_organizer()
    other = verified_organizer()
    own_report = _report(own)
    other_report = _report(other)
    moderator = _moderator()
    OrganizerMemberFactory.create(organizer=own, account=moderator)

    messages = _close(_client(moderator), [own_report, other_report], REASON)

    own_report.refresh_from_db()
    other_report.refresh_from_db()
    assert own_report.closed_at is None
    assert other_report.closed_at is not None
    assert messages == ["1 report(s) closed."]
    (event,) = AuditEvent.objects.filter(action=AuditAction.ORGANIZER_REPORT_CLOSE)
    assert event.target_object_id == str(other_report.pk)

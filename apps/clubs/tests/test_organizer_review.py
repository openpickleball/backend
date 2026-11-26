"""The moderator review counts for an Organizer (#271).

Over the last ``ORGANIZER_REVIEW_WINDOW_DAYS`` days, the disputes on the
Organizer's Events' matches: opened, withdrawn, resolved as confirmed or
voided, and expired, from the Audit Events the real workflow writes, and
the signal flags on those matches. Other Organizers' and self-reported
matches never count. Disputes are resolved by a member of the Event's
verified Organizer whose Player is not in the match (ADR 0034), since
moderators no longer resolve single matches (#273). Made-up data only.
"""

from datetime import date, timedelta

import pytest
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs import reports
from apps.clubs.models import Organizer, OrganizerReportReason
from apps.clubs.reports import close_report, organizer_review, report_organizer
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import FlagReason, Match, MatchFormat
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.matches.workflow import (
    confirm_match,
    dispute_match,
    expire_disputes_and_holds,
    resolve_dispute,
    withdraw_dispute,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up moderator reason."
SETTLE_REASON = "Made-up organizer reason."


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def _event(organizer: Organizer) -> Event:
    event: Event = EventFactory.create(organizer=organizer)
    return event


def _settler(event: Event) -> User:
    """A member of ``event``'s Organizer with no Player in its matches."""
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    return member


def _record(
    players: list[Player], event: Event, played_on: date, games: tuple[int, int]
) -> Match:
    member = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=(games, games),
        played_on=played_on,
        submitted_by_user=member,
        event=event,
    ).match


def _self_reported(players: list[Player], played_on: date) -> Match:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 1), (11, 2)),
        played_on=played_on,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


def _build(players: list[Player]) -> Organizer:
    """One of each dispute outcome on one Organizer's Events, plus noise."""
    organizer = verified_organizer()
    event = _event(organizer)
    second_event = _event(organizer)
    settler = _settler(event)
    disputer = players[0].account

    confirmed = _record(players, event, date(2026, 5, 1), (11, 3))
    dispute_match(confirmed, actor=disputer)
    resolve_dispute(confirmed, actor=settler, outcome="confirmed", reason=SETTLE_REASON)

    voided = _record(players, event, date(2026, 5, 2), (11, 4))
    dispute_match(voided, actor=disputer)
    resolve_dispute(voided, actor=settler, outcome="voided", reason=SETTLE_REASON)

    withdrawn = _record(players, second_event, date(2026, 5, 3), (11, 5))
    dispute_match(withdrawn, actor=disputer)
    withdraw_dispute(withdrawn, actor=disputer)

    expiring = _record(players, second_event, date(2026, 5, 4), (11, 6))
    dispute_match(expiring, actor=disputer)

    # Another Organizer's dispute, and a self-reported one: never counted.
    other_event = _event(verified_organizer())
    other = _record(players, other_event, date(2026, 5, 5), (11, 7))
    dispute_match(other, actor=disputer)
    resolve_dispute(
        other, actor=_settler(other_event), outcome="voided", reason=SETTLE_REASON
    )
    # A self-reported match has no Organizer level: its dispute is withdrawn.
    own = _self_reported(players, date(2026, 5, 6))
    dispute_match(own, actor=disputer)
    withdraw_dispute(own, actor=disputer)

    expire_disputes_and_holds(now=timezone.now() + timedelta(days=31))
    return organizer


def test_counts_each_dispute_outcome(players: list[Player]) -> None:
    organizer = _build(players)

    review = organizer_review(organizer)

    assert review.window_days == 90
    assert review.disputes_opened == 4
    assert review.disputes_withdrawn == 1
    assert review.disputes_confirmed == 1
    assert review.disputes_voided == 1
    assert review.disputes_expired == 1
    assert review.signal_flags == 0


def test_counts_equal_the_audit_events_on_its_events_matches(
    players: list[Player],
) -> None:
    organizer = _build(players)
    match_ids = [
        str(pk)
        for pk in Match.objects.filter(event__organizer=organizer).values_list(
            "pk", flat=True
        )
    ]
    events = AuditEvent.objects.filter(
        target_content_type=ContentType.objects.get_for_model(Match),
        target_object_id__in=match_ids,
    )

    review = organizer_review(organizer)

    assert (
        review.disputes_opened
        == events.filter(action=AuditAction.MATCH_DISPUTE).count()
    )
    assert (
        review.disputes_withdrawn
        == events.filter(action=AuditAction.MATCH_DISPUTE_WITHDRAW).count()
    )
    outcomes = [
        (e.after or {})["status"]
        for e in events.filter(action=AuditAction.DISPUTE_OUTCOME)
    ]
    assert review.disputes_confirmed == outcomes.count("confirmed")
    assert review.disputes_voided == outcomes.count("voided")
    expiries = [e.after or {} for e in events.filter(action=AuditAction.MATCH_EXPIRE)]
    assert review.disputes_expired == [a["rule"] for a in expiries].count("dispute")


def test_another_organizer_counts_only_its_own(players: list[Player]) -> None:
    _build(players)
    organizer = verified_organizer()

    review = organizer_review(organizer)

    assert review.disputes_opened == 0
    assert review.disputes_voided == 0


def test_events_before_the_window_are_left_out(players: list[Player]) -> None:
    organizer = _build(players)

    inside = organizer_review(organizer, now=timezone.now() + timedelta(days=89))
    outside = organizer_review(organizer, now=timezone.now() + timedelta(days=91))

    assert inside.disputes_opened == 4
    assert outside.disputes_opened == 0
    assert outside.disputes_expired == 0


@override_settings(ORGANIZER_REVIEW_WINDOW_DAYS=10)
def test_the_window_comes_from_settings(players: list[Player]) -> None:
    organizer = _build(players)
    now = timezone.now() + timedelta(days=11)

    review = organizer_review(organizer, now=now)

    assert review.window_days == 10
    assert review.since == now - timedelta(days=10)
    assert review.disputes_opened == 0


def test_signal_flags_count_reasons_in_the_signal_set(
    players: list[Player], monkeypatch: pytest.MonkeyPatch
) -> None:
    organizer = verified_organizer()
    event = _event(organizer)
    _record(players, event, date(2026, 5, 1), (11, 3))
    # Same teams and date, other scores: a possible duplicate flag.
    _record(players, event, date(2026, 5, 1), (11, 4))

    assert organizer_review(organizer).signal_flags == 0
    monkeypatch.setattr(
        reports, "SIGNAL_FLAG_REASONS", frozenset({FlagReason.POSSIBLE_DUPLICATE})
    )
    assert organizer_review(organizer).signal_flags == 1
    assert organizer_review(verified_organizer()).signal_flags == 0


def test_lists_only_open_reports_oldest_first() -> None:
    organizer = verified_organizer()
    first = report_organizer(
        organizer.pk,
        actor=PlayerFactory.create().account,
        reason=OrganizerReportReason.OTHER,
    )
    closed = report_organizer(
        organizer.pk,
        actor=PlayerFactory.create().account,
        reason=OrganizerReportReason.FAKE_MATCHES,
    )
    second = report_organizer(
        organizer.pk,
        actor=PlayerFactory.create().account,
        reason=OrganizerReportReason.WRONG_RESULTS,
    )
    close_report(closed, actor=_moderator(), reason=REASON)

    review = organizer_review(organizer)

    assert [r.pk for r in review.open_reports] == [first.pk, second.pk]

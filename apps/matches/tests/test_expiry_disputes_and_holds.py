"""Expiring unsettled disputes and possible duplicate holds (#268, ADR 0034).

A dispute no moderator resolves within ``MATCH_DISPUTE_EXPIRY_DAYS`` of
its first open dispute voids the match. A possible duplicate flag open
for ``MATCH_DUPLICATE_HOLD_DAYS`` voids the flag's ``match`` (the newer
one), so the earlier match counts again. Made-up data only.
"""

import logging
from datetime import date, datetime, timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.db import connection
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event
from apps.matches import workflow
from apps.matches.models import (
    ConfirmationDecision,
    FlagReason,
    FlagResolution,
    Match,
    MatchConfirmation,
    MatchFlag,
    MatchFormat,
    MatchStatus,
    Team,
)
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tasks import expire_disputes_and_holds as expire_task
from apps.matches.tests.conftest import (
    WORKFLOW_PLAYED_ON,
    SignalLog,
    audit_events_at,
)
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.tests.test_fix_service_lineup_locks import (
    _key,
    _other_session_holds,
)
from apps.matches.workflow import (
    confirm_match,
    dispute_match,
    dispute_started_at,
    expire_disputes_and_holds,
    resolve_dispute,
    resolve_flags,
    void_match,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from config import celery_app

pytestmark = pytest.mark.django_db

REASON = "Made-up review reason."
SECRET_TEXT = "made-up failure text"


@pytest.fixture(autouse=True)
def _default_periods(settings: Settings) -> None:
    settings.MATCH_DISPUTE_EXPIRY_DAYS = 30
    settings.MATCH_DUPLICATE_HOLD_DAYS = 14


def _days_from_now(days: int) -> datetime:
    return timezone.now() + timedelta(days=days)


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def _expire_events() -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(action=AuditAction.MATCH_EXPIRE).order_by("pk")
    )


def _counted() -> set[int]:
    return set(Match.objects.counted().values_list("pk", flat=True))


def _backdate_dispute(match: Match, player: Player, when: datetime) -> None:
    """Move one dispute row's time back. Raw SQL: the ORM refuses changes."""
    row = MatchConfirmation.objects.get(
        match=match, player=player, decision=ConfirmationDecision.DISPUTE
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE matches_matchconfirmation SET created_at = %s WHERE id = %s",
            [when, row.pk],
        )


def _backdate_flag(flag: MatchFlag, when: datetime) -> None:
    MatchFlag.objects.filter(pk=flag.pk).update(created_at=when)


def _near_duplicate(
    players: list[Player], games: tuple[tuple[int, int], ...] = ((11, 5), (11, 6))
) -> Match:
    """The same lineup and date as ``pending``, other scores: flagged."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def _confirm(match: Match, players: list[Player]) -> Match:
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


@pytest.fixture
def disputed(confirmed: Match, players: list[Player]) -> Match:
    return dispute_match(confirmed, actor=players[0].account)


@pytest.fixture
def pair(confirmed: Match, players: list[Player]) -> tuple[Match, Match]:
    """The earlier match and the newer one its possible duplicate flag holds."""
    later = _confirm(_near_duplicate(players), players)
    assert MatchFlag.objects.filter(match=later, duplicate_of=confirmed).exists()
    assert confirmed.pk not in _counted()
    return confirmed, later


# Dispute rule


def test_a_dispute_past_the_period_voids_the_match(
    disputed: Match, signals: SignalLog
) -> None:
    assert expire_disputes_and_holds(now=_days_from_now(31)) == 1

    assert _status(disputed) == MatchStatus.VOIDED
    [event] = _expire_events()
    assert event.target_object_id == str(disputed.pk)
    assert event.actor_user is None
    assert event.actor_application is None
    assert event.reason == ""
    assert event.before == {"status": MatchStatus.DISPUTED}
    assert event.after == {"status": MatchStatus.VOIDED, "rule": "dispute"}
    # A disputed match was not counting, so voiding it changes no rating.
    assert signals.changes(disputed) == []


def test_a_dispute_inside_the_period_is_left_alone(disputed: Match) -> None:
    assert expire_disputes_and_holds(now=_days_from_now(29)) == 0

    assert _status(disputed) == MatchStatus.DISPUTED
    assert _expire_events() == []


def test_the_period_follows_the_setting(disputed: Match, settings: Settings) -> None:
    settings.MATCH_DISPUTE_EXPIRY_DAYS = 3

    assert expire_disputes_and_holds(now=_days_from_now(4)) == 1
    assert _status(disputed) == MatchStatus.VOIDED


def test_a_dispute_resolved_as_confirmed_is_not_expired(
    disputed: Match, moderator: User
) -> None:
    resolve_dispute(
        disputed, actor=moderator, outcome=MatchStatus.CONFIRMED, reason=REASON
    )

    assert expire_disputes_and_holds(now=_days_from_now(31)) == 0
    assert _status(disputed) == MatchStatus.CONFIRMED


def test_a_new_dispute_after_a_resolution_starts_a_new_period(
    confirmed: Match, players: list[Player], moderator: User
) -> None:
    long_ago = timezone.now() - timedelta(days=40)
    with audit_events_at(long_ago):
        disputed = dispute_match(confirmed, actor=players[0].account)
    _backdate_dispute(disputed, players[0], long_ago)
    assert dispute_started_at(disputed) == long_ago
    resolve_dispute(
        disputed, actor=moderator, outcome=MatchStatus.CONFIRMED, reason=REASON
    )
    dispute_match(disputed, actor=players[2].account)
    started = dispute_started_at(disputed)
    assert started is not None
    assert started > long_ago + timedelta(days=39)

    # Past the first dispute's period, inside the second's.
    assert expire_disputes_and_holds(now=_days_from_now(1)) == 0
    assert _status(disputed) == MatchStatus.DISPUTED

    assert expire_disputes_and_holds(now=_days_from_now(31)) == 1
    assert _status(disputed) == MatchStatus.VOIDED


def test_the_period_runs_from_the_first_open_dispute(
    confirmed: Match, players: list[Player]
) -> None:
    first = timezone.now() - timedelta(days=31)
    with audit_events_at(first):
        disputed = dispute_match(confirmed, actor=players[0].account)
    # A second participant joins the dispute later: no new period.
    dispute_match(disputed, actor=players[2].account)

    assert dispute_started_at(disputed) == first
    assert expire_disputes_and_holds() == 1
    assert _status(disputed) == MatchStatus.VOIDED


def test_no_dispute_has_no_start(confirmed: Match) -> None:
    assert dispute_started_at(confirmed) is None


def test_other_statuses_are_left_alone(
    pending: Match, players: list[Player], moderator: User
) -> None:
    dispute_match(pending, actor=players[0].account)
    resolve_dispute(pending, actor=moderator, outcome=MatchStatus.VOIDED, reason=REASON)
    events = len(_expire_events())

    assert expire_disputes_and_holds(now=_days_from_now(31)) == 0
    assert len(_expire_events()) == events


def test_a_second_run_voids_nothing_more(disputed: Match) -> None:
    later = _days_from_now(31)
    assert expire_disputes_and_holds(now=later) == 1

    assert expire_disputes_and_holds(now=later) == 0
    assert len(_expire_events()) == 1


def test_a_disputed_match_that_held_another_releases_it(
    pair: tuple[Match, Match], players: list[Player], signals: SignalLog
) -> None:
    earlier, later = pair
    dispute_match(later, actor=players[0].account)

    # Past both periods: the dispute rule runs first and voids it.
    assert expire_disputes_and_holds(now=_days_from_now(31)) == 1

    assert _status(later) == MatchStatus.VOIDED
    assert earlier.pk in _counted()
    assert signals.changes(earlier) == [MatchChange.RELEASE]
    [event] = _expire_events()
    assert event.after == {"status": MatchStatus.VOIDED, "rule": "dispute"}


# Possible duplicate hold rule


def test_a_hold_past_the_period_voids_the_newer_match(
    pair: tuple[Match, Match], signals: SignalLog
) -> None:
    earlier, later = pair

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    assert _status(later) == MatchStatus.VOIDED
    assert _status(earlier) == MatchStatus.CONFIRMED
    assert earlier.pk in _counted()
    assert signals.changed == [
        (earlier.pk, MatchChange.RELEASE, date(2026, 6, 1)),
    ]
    [event] = _expire_events()
    assert event.target_object_id == str(later.pk)
    assert event.actor_user is None
    assert event.actor_application is None
    assert event.before == {"status": MatchStatus.CONFIRMED}
    assert event.after == {"status": MatchStatus.VOIDED, "rule": "duplicate_hold"}


def test_a_hold_inside_the_period_is_left_alone(pair: tuple[Match, Match]) -> None:
    _, later = pair

    assert expire_disputes_and_holds(now=_days_from_now(13)) == 0

    assert _status(later) == MatchStatus.CONFIRMED
    assert _expire_events() == []


def test_the_hold_period_follows_the_setting(
    pair: tuple[Match, Match], settings: Settings
) -> None:
    settings.MATCH_DUPLICATE_HOLD_DAYS = 2
    _, later = pair

    assert expire_disputes_and_holds(now=_days_from_now(3)) == 1
    assert _status(later) == MatchStatus.VOIDED


def test_the_hold_period_runs_from_the_flag(pair: tuple[Match, Match]) -> None:
    _, later = pair
    _backdate_flag(
        MatchFlag.objects.get(match=later), timezone.now() - timedelta(days=15)
    )

    assert expire_disputes_and_holds() == 1
    assert _status(later) == MatchStatus.VOIDED


def test_a_resolved_flag_voids_nothing(
    pair: tuple[Match, Match], moderator: User
) -> None:
    _, later = pair
    resolve_flags(
        later, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
    )

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 0
    assert _status(later) == MatchStatus.CONFIRMED


def test_a_flag_on_a_voided_match_voids_nothing(
    pair: tuple[Match, Match], moderator: User
) -> None:
    earlier, later = pair
    void_match(later, actor=moderator, reason=REASON)
    events = len(_expire_events())

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 0
    assert _status(earlier) == MatchStatus.CONFIRMED
    assert len(_expire_events()) == events


def test_a_flag_whose_earlier_match_is_voided_voids_nothing(
    pair: tuple[Match, Match], moderator: User
) -> None:
    earlier, later = pair
    void_match(earlier, actor=moderator, reason=REASON)

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 0
    assert _status(later) == MatchStatus.CONFIRMED


def test_a_second_hold_run_voids_nothing_more(pair: tuple[Match, Match]) -> None:
    later_time = _days_from_now(15)
    assert expire_disputes_and_holds(now=later_time) == 1

    assert expire_disputes_and_holds(now=later_time) == 0
    assert len(_expire_events()) == 1


def test_both_rules_in_one_run(
    pair: tuple[Match, Match], players: list[Player], moderator: User
) -> None:
    earlier, later = pair
    resolve_flags(
        later, actor=moderator, outcome=FlagResolution.DISMISSED, reason=REASON
    )
    # A new near duplicate holds both again; a dispute on the earlier one.
    newest = _near_duplicate(players, games=((11, 2), (11, 3)))
    dispute_match(earlier, actor=players[0].account)

    assert expire_disputes_and_holds(now=_days_from_now(31)) == 2

    assert _status(earlier) == MatchStatus.VOIDED
    assert _status(newest) == MatchStatus.VOIDED
    assert _status(later) == MatchStatus.CONFIRMED
    rules = sorted(str((event.after or {})["rule"]) for event in _expire_events())
    assert rules == ["dispute", "duplicate_hold"]


# Locks and failures


def _factory_match(status: MatchStatus) -> Match:
    # Built by the factory, so no earlier call in the test took its lineup
    # lock (the other session would wait on this one).
    made: Match = MatchFactory.create()
    raw_update(made, status=status)
    made.refresh_from_db()
    return made


def test_a_match_whose_lineup_lock_is_held_is_skipped() -> None:
    disputed = _factory_match(MatchStatus.DISPUTED)
    MatchConfirmation.objects.create(
        match=disputed,
        player=disputed.participants.get(team=Team.B).player,
        decision=ConfirmationDecision.DISPUTE,
    )
    # What dispute_match records, written directly so this session takes
    # no lineup lock.
    record_event(
        action=AuditAction.MATCH_DISPUTE,
        target=disputed,
        before={"status": MatchStatus.CONFIRMED},
        after={"status": MatchStatus.DISPUTED},
    )
    later = _days_from_now(31)

    with _other_session_holds(_key(disputed)):
        assert expire_disputes_and_holds(now=later) == 0
        assert _status(disputed) == MatchStatus.DISPUTED

    assert expire_disputes_and_holds(now=later) == 1
    assert _status(disputed) == MatchStatus.VOIDED


def test_a_held_match_whose_lineup_lock_is_held_is_skipped() -> None:
    earlier = _factory_match(MatchStatus.CONFIRMED)
    later = _factory_match(MatchStatus.CONFIRMED)
    MatchFlag.objects.create(
        match=later, reason=FlagReason.POSSIBLE_DUPLICATE, duplicate_of=earlier
    )
    when = _days_from_now(15)

    with _other_session_holds(_key(later)):
        assert expire_disputes_and_holds(now=when) == 0
        assert _status(later) == MatchStatus.CONFIRMED

    assert expire_disputes_and_holds(now=when) == 1
    assert _status(later) == MatchStatus.VOIDED


def test_a_failing_match_is_skipped_and_reported(
    pair: tuple[Match, Match],
    disputed_other: Match,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, later = pair
    real = workflow._void

    def void_or_fail(locked: Match, **kwargs: object) -> None:
        if locked.pk == disputed_other.pk:
            raise RuntimeError(SECRET_TEXT)
        real(locked, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workflow, "_void", void_or_fail)

    with (
        caplog.at_level(logging.ERROR, logger="apps.matches.workflow"),
        pytest.raises(workflow.ExpiryFailed) as error,
    ):
        expire_disputes_and_holds(now=_days_from_now(31))

    # The failing match rolled back alone; the other rule still ran.
    assert _status(disputed_other) == MatchStatus.DISPUTED
    assert _status(later) == MatchStatus.VOIDED
    assert error.value.failed == 1
    assert str(error.value) == "1 matches could not be expired."
    messages = [record.getMessage() for record in caplog.records]
    assert any(str(disputed_other.pk) in message for message in messages)
    assert all(SECRET_TEXT not in message for message in messages)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.fixture
def disputed_other() -> Match:
    """A disputed match with another lineup than ``pair``."""
    others = [PlayerFactory.create() for _ in range(2)]
    match = record_match(
        format=MatchFormat.SINGLES,
        team_a=others[:1],
        team_b=others[1:],
        games=((11, 3),),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=others[0].account,
    ).match
    return dispute_match(match, actor=others[1].account)


def test_the_soft_time_limit_stops_the_run(
    disputed: Match, monkeypatch: pytest.MonkeyPatch
) -> None:
    def out_of_time(locked: Match, **kwargs: object) -> None:
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(workflow, "_void", out_of_time)

    with pytest.raises(SoftTimeLimitExceeded):
        expire_disputes_and_holds(now=_days_from_now(31))


# Task


def test_the_task_returns_the_count(confirmed: Match, players: list[Player]) -> None:
    with audit_events_at(timezone.now() - timedelta(days=31)):
        dispute_match(confirmed, actor=players[0].account)

    assert expire_task.delay().get() == 1


def test_the_task_has_an_explicit_name_and_the_default_queue() -> None:
    assert expire_task.name == "apps.matches.tasks.expire_disputes_and_holds"
    route = celery_app.amqp.router.route({}, expire_task.name)
    assert route["queue"].name == "default"


def test_the_task_stays_under_the_visibility_timeout() -> None:
    timeout = celery_app.conf.broker_transport_options["visibility_timeout"]

    assert expire_task.time_limit is not None
    assert expire_task.soft_time_limit is not None
    assert expire_task.soft_time_limit < expire_task.time_limit < timeout


def test_beat_runs_the_task_daily() -> None:
    [entry] = [
        entry
        for entry in celery_app.conf.beat_schedule.values()
        if entry["task"] == expire_task.name
    ]

    schedule = entry["schedule"]
    assert schedule.hour == {3}
    assert schedule.minute == {30}
    assert entry["options"] == {"expire_seconds": 3600}

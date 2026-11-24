"""``record_match`` and confirmation (#26, decisions F1 and F4).

A match with an Event is created confirmed; a self-submission is created
pending. A permitted non-self submitter (event authority, ADR 0021) whose
exact duplicate matches a pending match confirms that match. A match with
a Club and no Event is refused, so it never confirms one. Made-up data only.
"""

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import Club
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.permissions import MatchPermissionDenied
from apps.matches.services import (
    DuplicateMatchError,
    RecordedMatch,
    record_match,
)
from apps.matches.signals import MatchChange, match_confirmed
from apps.matches.tests.conftest import SignalLog
from apps.matches.tests.factories import raw_update
from apps.partners.tests.factories import approved_application
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (9, 11), (11, 4))
OTHER_GAMES = ((11, 5), (11, 8))


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(2)]


@pytest.fixture
def club() -> Club:
    made: Club = ClubFactory.create()
    return made


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _club_account(club: Club) -> User:
    account: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=account)
    return account


def _event_member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _record(players: Sequence[Player], **overrides: Any) -> RecordedMatch:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": players[:1],
        "team_b": players[1:],
        "games": GAMES,
        "played_on": PLAYED_ON,
    }
    values.update(overrides)
    return record_match(**values)


def _self_submit(players: Sequence[Player], **overrides: Any) -> RecordedMatch:
    return _record(players, submitted_by_user=players[0].account, **overrides)


def _events(match: Match, action: AuditAction) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(action=action, target_object_id=str(match.pk))
    )


def test_a_self_submission_is_created_pending(
    players: list[Player], signals: SignalLog
) -> None:
    recorded = _self_submit(players)

    assert recorded.created
    assert recorded.match.status == MatchStatus.PENDING
    assert Match.objects.get(pk=recorded.match.pk).status == MatchStatus.PENDING
    assert not AuditEvent.objects.filter(action=AuditAction.MATCH_CONFIRM).exists()
    assert signals.confirmed == []


def test_an_event_and_club_submission_is_created_confirmed(
    players: list[Player], event: Event, club: Club, signals: SignalLog
) -> None:
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    account = _event_member(event)

    recorded = _record(players, submitted_by_user=account, event=event, club=club)

    match = recorded.match
    assert recorded.created
    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    [event] = _events(match, AuditAction.MATCH_CONFIRM)
    assert event.actor_user == account
    assert event.actor_application is None
    assert event.before is None
    assert event.after == {"status": MatchStatus.CONFIRMED}
    assert AuditEvent.objects.count() == 1 + _verification_events()
    assert signals.confirmed == [match.pk]


def _verification_events() -> int:
    return AuditEvent.objects.filter(action=AuditAction.ORGANIZER_VERIFY).count()


def test_an_application_event_submission_is_created_confirmed(
    players: list[Player], event: Event, signals: SignalLog
) -> None:
    application = approved_application(organizer=event.organizer)

    recorded = _record(players, submitted_by_application=application, event=event)

    match = recorded.match
    assert match.status == MatchStatus.CONFIRMED
    [audit] = _events(match, AuditAction.MATCH_CONFIRM)
    assert audit.actor_application == application
    assert audit.actor_user is None
    assert signals.confirmed == [match.pk]


def test_an_organizer_duplicate_confirms_a_pending_self_submission(
    players: list[Player], event: Event, signals: SignalLog
) -> None:
    pending = _self_submit(players).match
    member = _event_member(event)
    count = Match.objects.count()

    recorded = _record(players, submitted_by_user=member, event=event)

    assert not recorded.created
    assert recorded.match.pk == pending.pk
    assert Match.objects.count() == count
    pending.refresh_from_db()
    assert pending.status == MatchStatus.CONFIRMED
    assert pending.event is None
    [audit] = _events(pending, AuditAction.MATCH_CONFIRM)
    assert audit.actor_user == member
    assert audit.before == {"status": MatchStatus.PENDING}
    assert audit.after == {"status": MatchStatus.CONFIRMED}
    assert signals.confirmed == [pending.pk]


def test_a_club_only_duplicate_no_longer_confirms_a_pending_self_submission(
    players: list[Player], club: Club, signals: SignalLog
) -> None:
    # ADR 0021: club authority is gone, so a Club admin's club-only
    # resubmission is refused instead of confirming the pending match.
    pending = _self_submit(players, games=tuple((b, a) for a, b in GAMES)).match
    events = AuditEvent.objects.count()

    with pytest.raises(MatchPermissionDenied) as error:
        _record(
            list(reversed(players)), submitted_by_user=_club_account(club), club=club
        )

    assert str(error.value) == MatchPermissionDenied.message
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING
    assert AuditEvent.objects.count() == events
    assert signals.confirmed == []


def test_an_event_duplicate_with_a_granted_club_confirms_a_pending_match(
    players: list[Player], event: Event, club: Club, signals: SignalLog
) -> None:
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    pending = _self_submit(players).match

    recorded = _record(
        players, submitted_by_user=_event_member(event), event=event, club=club
    )

    assert not recorded.created
    assert recorded.match.pk == pending.pk
    stored = Match.objects.get(pk=pending.pk)
    assert stored.status == MatchStatus.CONFIRMED
    assert stored.club_id is None
    assert signals.confirmed == [pending.pk]


def test_a_self_submitted_duplicate_of_a_pending_match_is_refused(
    players: list[Player], signals: SignalLog
) -> None:
    pending = _self_submit(players).match

    with pytest.raises(DuplicateMatchError):
        _record(players, submitted_by_user=players[1].account)

    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING
    assert signals.confirmed == []


@pytest.mark.parametrize("status", [MatchStatus.CONFIRMED, MatchStatus.DISPUTED])
def test_a_permitted_duplicate_of_another_status_is_refused(
    players: list[Player], event: Event, signals: SignalLog, status: MatchStatus
) -> None:
    earlier = _self_submit(players).match
    raw_update(earlier, status=status)
    events = AuditEvent.objects.count()

    with pytest.raises(DuplicateMatchError):
        _record(players, submitted_by_user=_event_member(event), event=event)

    assert Match.objects.get(pk=earlier.pk).status == status
    assert AuditEvent.objects.count() == events
    assert signals.confirmed == []


def test_a_near_duplicate_holds_a_counting_match(
    players: list[Player], event: Event, signals: SignalLog
) -> None:
    account = _event_member(event)
    earlier = _record(players, submitted_by_user=account, event=event).match
    assert earlier.is_counted()

    later = _record(
        players, submitted_by_user=account, event=event, games=OTHER_GAMES
    ).match

    assert not earlier.is_counted()
    assert not later.is_counted()
    assert signals.changed == [(earlier.pk, MatchChange.HOLD, PLAYED_ON)]
    assert signals.confirmed == [earlier.pk, later.pk]


def test_a_near_duplicate_of_a_pending_match_sends_no_hold(
    players: list[Player], signals: SignalLog
) -> None:
    _self_submit(players)

    _self_submit(players, games=OTHER_GAMES)

    assert signals.changed == []


def test_a_failing_receiver_rolls_the_match_back(
    players: list[Player], event: Event
) -> None:
    def fail(**kwargs: Any) -> None:
        raise RuntimeError("receiver failed")

    match_confirmed.connect(fail, weak=False)
    count = Match.objects.count()
    try:
        with pytest.raises(RuntimeError):
            _record(players, submitted_by_user=_event_member(event), event=event)
    finally:
        match_confirmed.disconnect(fail)

    assert Match.objects.count() == count
    assert not AuditEvent.objects.filter(action=AuditAction.MATCH_CONFIRM).exists()

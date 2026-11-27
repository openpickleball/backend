"""A held member has no Organizer authority (#232, ADR 0057). Made-up
data only.

While an Organizer Member's ``hold_ends_at`` is in the future (the New
Member Hold), every rule that grants Organizer authority, and every
query that mirrors one, leaves the member out. Once the time passes the
member acts as any other.
"""

from datetime import date, timedelta
from typing import Any

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole, OrganizerMember
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import FlagReason, Match, MatchFlag, MatchFormat
from apps.matches.permissions import (
    MatchPermissionDenied,
    MatchSubmission,
    VouchLevel,
    can_change_match,
    can_confirm_as_organizer,
    can_create_match,
    can_manage_events,
    settles_dispute,
    vouch_level,
)
from apps.matches.services import record_match
from apps.matches.workflow import dispute_match, organizer_disputes, vouchable_holds
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (11, 9))


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _member(event: Event, *, held: bool) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer,
        account=account,
        role=MemberRole.MATCH_MANAGER,
        hold_ends_at=timezone.now() + timedelta(hours=5) if held else None,
    )
    return account


def _release(account: User) -> None:
    OrganizerMember.objects.filter(account=account).update(
        hold_ends_at=timezone.now() - timedelta(seconds=1)
    )


def _record(
    players: list[Player],
    event: Event | None,
    submitter: User,
    games: tuple[tuple[int, int], ...] = GAMES,
) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=PLAYED_ON,
        submitted_by_user=submitter,
        event=event,
    ).match


def _event_match(players: list[Player], event: Event) -> Match:
    return _record(players, event, _member(event, held=False))


def _submission(players: list[Player], event: Event) -> MatchSubmission:
    return MatchSubmission(
        event=event, club=None, player_ids=frozenset(p.pk for p in players)
    )


def test_a_held_member_cannot_record_a_match(
    players: list[Player], event: Event
) -> None:
    member = _member(event, held=True)

    assert not can_create_match(member, _submission(players, event))
    with pytest.raises(MatchPermissionDenied):
        _record(players, event, member)
    _release(member)
    assert can_create_match(member, _submission(players, event))


@pytest.mark.parametrize(
    "rule",
    [can_change_match, can_confirm_as_organizer, settles_dispute],
    ids=["change", "confirm_as_organizer", "settles_dispute"],
)
def test_a_held_member_has_no_match_authority(
    players: list[Player], event: Event, rule: Any
) -> None:
    match = _event_match(players, event)
    member = _member(event, held=True)

    assert not rule(member, match)
    _release(member)
    assert rule(member, match)


def test_a_held_member_cannot_manage_events(event: Event) -> None:
    member = _member(event, held=True)

    assert not can_manage_events(member, event.organizer_id)
    _release(member)
    assert can_manage_events(member, event.organizer_id)


def test_a_held_member_has_no_vouch_level(players: list[Player], event: Event) -> None:
    match = _event_match(players, event)
    member = _member(event, held=True)

    assert vouch_level(member, match) is None
    _release(member)
    assert vouch_level(member, match) == VouchLevel.ORGANIZER


def test_a_held_member_lists_no_disputes(players: list[Player], event: Event) -> None:
    match = dispute_match(_event_match(players, event), actor=players[0].account)
    member = _member(event, held=True)

    assert list(organizer_disputes(member)) == []
    _release(member)
    assert list(organizer_disputes(member)) == [match]


def test_a_held_member_lists_no_holds(players: list[Player], event: Event) -> None:
    earlier = _event_match(players, event)
    later = _record(players, event, _member(event, held=False), ((11, 5), (11, 5)))
    assert MatchFlag.objects.filter(
        match=later, duplicate_of=earlier, reason=FlagReason.POSSIBLE_DUPLICATE
    ).exists()
    member = _member(event, held=True)

    assert list(vouchable_holds(member)) == []
    _release(member)
    assert set(vouchable_holds(member)) == {earlier, later}


def test_a_held_member_gets_no_dispute_notice(
    players: list[Player],
    event: Event,
    django_capture_on_commit_callbacks: Any,
) -> None:
    match = _event_match(players, event)
    held = _member(event, held=True)
    free = _member(event, held=False)
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(match, actor=players[0].account)

    recipients = {address for message in mail.outbox for address in message.to}
    assert free.email in recipients
    assert held.email not in recipients

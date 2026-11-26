"""Organizer authority needs MFA (issue #99, ADR 0043). Made-up data only.

A member of a verified Organizer acts for it only while the member's
Account has a method (``apps.accounts.mfa.has_mfa``). Every rule that
grants Organizer authority, and every query that mirrors one, agrees. A
player's self-submission needs no MFA.
"""

from datetime import date
from typing import Any

import pytest
from django.core import mail

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    FlagReason,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
)
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


def _member(event: Event, *, mfa: bool) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer,
        account=account,
        role=MemberRole.MATCH_MANAGER,
        mfa=mfa,
    )
    return account


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
    return _record(players, event, _member(event, mfa=True))


def _submission(players: list[Player], event: Event) -> MatchSubmission:
    return MatchSubmission(
        event=event, club=None, player_ids=frozenset(p.pk for p in players)
    )


def test_a_member_without_mfa_cannot_record_a_match(
    players: list[Player], event: Event
) -> None:
    member = _member(event, mfa=False)

    assert not can_create_match(member, _submission(players, event))
    with pytest.raises(MatchPermissionDenied):
        _record(players, event, member)
    assert not Match.objects.exists()


def test_the_member_records_it_once_enrolled(
    players: list[Player], event: Event
) -> None:
    member = _member(event, mfa=False)
    enroll_totp(member)

    match = _record(players, event, member)

    assert match.status == MatchStatus.CONFIRMED


def test_recovery_codes_alone_give_no_authority(
    players: list[Player], event: Event
) -> None:
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    member = _member(event, mfa=False)
    RecoveryCodes.activate(member)

    assert not can_create_match(member, _submission(players, event))


def test_a_player_self_submits_without_mfa(players: list[Player]) -> None:
    match = _record(players, None, players[0].account)

    assert match.status == MatchStatus.PENDING


@pytest.mark.parametrize(
    "rule",
    [can_change_match, can_confirm_as_organizer, settles_dispute],
    ids=["change", "confirm_as_organizer", "settles_dispute"],
)
def test_a_member_without_mfa_has_no_match_authority(
    players: list[Player], event: Event, rule: Any
) -> None:
    match = _event_match(players, event)
    member = _member(event, mfa=False)

    assert not rule(member, match)
    enroll_totp(member)
    assert rule(member, match)


def test_a_member_without_mfa_cannot_manage_events(event: Event) -> None:
    member = _member(event, mfa=False)

    assert not can_manage_events(member, event.organizer_id)
    enroll_totp(member)
    assert can_manage_events(member, event.organizer_id)


def test_a_member_without_mfa_has_no_vouch_level(
    players: list[Player], event: Event
) -> None:
    match = _event_match(players, event)
    member = _member(event, mfa=False)

    assert vouch_level(member, match) is None
    enroll_totp(member)
    assert vouch_level(member, match) == VouchLevel.ORGANIZER


def test_a_member_without_mfa_lists_no_disputes(
    players: list[Player], event: Event
) -> None:
    match = dispute_match(_event_match(players, event), actor=players[0].account)
    member = _member(event, mfa=False)

    assert list(organizer_disputes(member)) == []
    enroll_totp(member)
    assert list(organizer_disputes(member)) == [match]


def test_a_member_without_mfa_lists_no_holds(
    players: list[Player], event: Event
) -> None:
    earlier = _event_match(players, event)
    # Same teams and date, other scores: a near duplicate holds both.
    later = _record(players, event, _member(event, mfa=True), ((11, 5), (11, 5)))
    assert MatchFlag.objects.filter(
        match=later, duplicate_of=earlier, reason=FlagReason.POSSIBLE_DUPLICATE
    ).exists()
    member = _member(event, mfa=False)

    assert list(vouchable_holds(member)) == []
    enroll_totp(member)
    assert set(vouchable_holds(member)) == {earlier, later}


def test_a_member_without_mfa_gets_no_dispute_notice(
    players: list[Player],
    event: Event,
    django_capture_on_commit_callbacks: Any,
) -> None:
    match = _event_match(players, event)
    without = _member(event, mfa=False)
    with_mfa = _member(event, mfa=True)
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        dispute_match(match, actor=players[0].account)

    recipients = {address for message in mail.outbox for address in message.to}
    assert with_mfa.email in recipients
    assert without.email not in recipients

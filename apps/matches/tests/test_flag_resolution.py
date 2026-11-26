"""A resolved flag holds nothing (#40, #270). Made-up data only.

Flags are resolved only by vouches from both sides (``vouch_for_match``,
#273). A resolved flag no longer holds either match from counting and no
longer links them, so a later void of one sends nothing for the other.
The flow of vouching itself is tested in ``test_vouch.py``.
"""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import (
    FlagReason,
    FlagResolution,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
)
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON, SignalLog
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import _linked, confirm_match, void_match, vouch_for_match
from apps.players.models import Player

pytestmark = pytest.mark.django_db


def _near_duplicate(players: list[Player]) -> Match:
    """The same lineup and date as ``pending``, other scores: flagged."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def _confirm(match: Match, players: list[Player]) -> Match:
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


@pytest.fixture
def pair(confirmed: Match, players: list[Player]) -> tuple[Match, Match]:
    """Two confirmed matches a possible duplicate flag holds."""
    later = _confirm(_near_duplicate(players), players)
    assert MatchFlag.objects.filter(match=later, duplicate_of=confirmed).exists()
    assert not confirmed.is_counted()
    assert not later.is_counted()
    return confirmed, later


def _vouch_both_teams(match: Match, players: list[Player]) -> None:
    vouch_for_match(match, actor=players[0].account)
    vouch_for_match(match, actor=players[3].account)


def _counted() -> set[int]:
    return set(Match.objects.counted().values_list("pk", flat=True))


def test_a_new_flag_is_open(pair: tuple[Match, Match]) -> None:
    flag = MatchFlag.objects.get()

    assert flag.resolution == ""
    assert flag.resolved_at is None


def test_resolution_and_time_are_set_together(pair: tuple[Match, Match]) -> None:
    flag = MatchFlag.objects.get()
    flag.resolution = FlagResolution.APPROVED

    with pytest.raises(IntegrityError), transaction.atomic():
        flag.save()


def test_time_without_resolution_is_refused(pair: tuple[Match, Match]) -> None:
    flag = MatchFlag.objects.get()
    flag.resolved_at = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        flag.save()


def test_counted_ignores_a_resolved_flag(pair: tuple[Match, Match]) -> None:
    earlier, later = pair
    flag = MatchFlag.objects.get()
    flag.resolution = FlagResolution.APPROVED
    flag.resolved_at = timezone.now()
    flag.save()

    assert _counted() == {earlier.pk, later.pk}


def test_linked_ignores_resolved_flags(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, later = pair
    assert [m.pk for m in _linked(later)] == [earlier.pk]

    _vouch_both_teams(earlier, players)
    _vouch_both_teams(later, players)

    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED
    assert _linked(later) == []
    assert _linked(earlier) == []


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=account, role=MemberRole.MATCH_MANAGER
    )
    return account


def test_voiding_after_resolution_sends_no_signal_for_the_other(
    event: Event, players: list[Player], signals: SignalLog
) -> None:
    member = _member(event)

    def record(games: tuple[tuple[int, int], ...]) -> Match:
        return record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=games,
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=member,
            event=event,
        ).match

    earlier = record(((11, 7), (11, 9)))
    later = record(((11, 5), (11, 6)))
    assert MatchFlag.objects.filter(match=later, duplicate_of=earlier).exists()
    vouch_for_match(earlier, actor=member)
    vouch_for_match(later, actor=member)
    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED
    signals.changed.clear()

    void_match(later, actor=member)

    assert signals.changes(later) == [MatchChange.VOID]
    assert signals.changes(earlier) == []
    assert _counted() == {earlier.pk}


def test_an_open_flag_still_holds_a_voided_partner_rule(
    pair: tuple[Match, Match],
) -> None:
    """Unchanged: a voided match stops holding the other (ADR 0013)."""
    earlier, later = pair
    raw_update(later, status=MatchStatus.VOIDED)

    assert _counted() == {earlier.pk}
    assert MatchFlag.objects.get().reason == FlagReason.POSSIBLE_DUPLICATE

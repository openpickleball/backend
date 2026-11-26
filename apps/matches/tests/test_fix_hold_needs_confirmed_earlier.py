"""The hold rule needs a confirmed earlier match (#268, review fix).

The maintainer decided (#268, #265): an open possible duplicate flag
voids its newer match (the flag's ``match``) after the hold period only
when the earlier match (``duplicate_of``) is confirmed. Otherwise a
player's own pending self-submission could void the verified Organizer's
real result just by waiting. A correction keeps its original's place
in that order (maintainer's follow-up decision on #268). The corrections
here are made by a member of the match's Event's verified Organizer, or
by a player for a self-reported match (#267). Made-up data only.
"""

from datetime import date, datetime, timedelta

import pytest
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFlag, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON, SignalLog
from apps.matches.workflow import (
    _expire_hold,
    confirm_match,
    dispute_match,
    edit_match,
    expire_disputes_and_holds,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

OTHER_GAMES = ((11, 5), (11, 6))


@pytest.fixture(autouse=True)
def _default_periods(settings: Settings) -> None:
    settings.MATCH_DISPUTE_EXPIRY_DAYS = 30
    settings.MATCH_DUPLICATE_HOLD_DAYS = 14


def _days_from_now(days: int) -> datetime:
    return timezone.now() + timedelta(days=days)


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def _expire_events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.MATCH_EXPIRE))


def _counted() -> set[int]:
    return set(Match.objects.counted().values_list("pk", flat=True))


def _near_duplicate(players: list[Player]) -> Match:
    """The same lineup and date as ``pending``, other scores: flagged."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=OTHER_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def _confirm(match: Match, players: list[Player]) -> Match:
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


@pytest.fixture
def member(event: Event) -> User:
    """A member of ``event``'s verified Organizer, with no Player."""
    account: User = OrganizerMemberFactory.create(organizer=event.organizer).account
    return account


def _event_match(
    event: Event,
    member: User,
    players: list[Player],
    games: tuple[tuple[int, int], ...] = WORKFLOW_GAMES,
) -> Match:
    """``players``' lineup on WORKFLOW_PLAYED_ON at ``event``: confirmed."""
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        event=event,
        submitted_by_user=member,
    ).match
    assert match.status == MatchStatus.CONFIRMED
    return match


@pytest.fixture
def organizer_confirmed(event: Event, member: User, players: list[Player]) -> Match:
    """The Organizer's confirmed result on the lineup, recorded first."""
    return _event_match(event, member, players)


def test_a_pending_self_submission_never_voids_the_organizers_result(
    pending: Match, players: list[Player], event: Event
) -> None:
    member: User = OrganizerMemberFactory.create(organizer=event.organizer).account
    real = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=OTHER_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        event=event,
        submitted_by_user=member,
    ).match
    assert real.status == MatchStatus.CONFIRMED
    assert MatchFlag.objects.filter(match=real, duplicate_of=pending).exists()

    assert expire_disputes_and_holds(now=_days_from_now(60)) == 0

    assert _status(real) == MatchStatus.CONFIRMED
    assert _expire_events() == []


def test_a_disputed_earlier_match_voids_nothing_until_its_dispute_expires(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    later = _confirm(_near_duplicate(players), players)
    dispute_match(confirmed, actor=players[0].account)

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 0
    assert _status(later) == MatchStatus.CONFIRMED
    assert _status(confirmed) == MatchStatus.DISPUTED

    signals.changed.clear()
    assert expire_disputes_and_holds(now=_days_from_now(31)) == 1

    assert _status(confirmed) == MatchStatus.VOIDED
    assert _status(later) == MatchStatus.CONFIRMED
    assert later.pk in _counted()
    assert signals.changes(later) == [MatchChange.RELEASE]


def _correct(
    match: Match,
    players: list[Player],
    member: User,
    games: tuple[tuple[int, int], ...] = WORKFLOW_GAMES,
) -> Match:
    """The Organizer member's correction of the Event match ``match``."""
    return edit_match(
        match,
        actor=member,
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
    )


# A correction keeps its original's place (maintainer's decision on #268):
# a correction of a match recorded before the flag's other match is the
# earlier one, and the other match is the newer one the hold rule voids.


def test_a_confirmed_correction_of_an_older_match_stays_and_the_other_is_voided(
    organizer_confirmed: Match,
    players: list[Player],
    member: User,
    signals: SignalLog,
) -> None:
    confirmed_dup = _confirm(_near_duplicate(players), players)
    correction = _correct(organizer_confirmed, players, member)
    assert correction.status == MatchStatus.CONFIRMED
    assert MatchFlag.objects.filter(
        match=correction, duplicate_of=confirmed_dup
    ).exists()

    signals.changed.clear()
    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    assert _status(confirmed_dup) == MatchStatus.VOIDED
    assert _status(correction) == MatchStatus.CONFIRMED
    assert correction.pk in _counted()
    assert signals.changes(correction) == [MatchChange.RELEASE]
    [event] = _expire_events()
    assert event.target_object_id == str(confirmed_dup.pk)
    assert event.after is not None
    assert event.after["rule"] == "duplicate_hold"

    # A rerun voids nothing more.
    assert expire_disputes_and_holds(now=_days_from_now(16)) == 0
    assert _status(correction) == MatchStatus.CONFIRMED


def test_a_pending_match_held_by_a_confirmed_correction_of_an_older_match_is_voided(
    organizer_confirmed: Match, players: list[Player], member: User
) -> None:
    pending_dup = _near_duplicate(players)
    correction = _correct(organizer_confirmed, players, member)
    assert MatchFlag.objects.filter(match=correction, duplicate_of=pending_dup).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    assert _status(pending_dup) == MatchStatus.VOIDED
    assert _status(correction) == MatchStatus.CONFIRMED
    assert correction.pk in _counted()


def test_a_correction_of_a_correction_keeps_the_first_matchs_place(
    organizer_confirmed: Match, players: list[Player], member: User
) -> None:
    first = _correct(organizer_confirmed, players, member, games=((11, 1), (11, 2)))
    confirmed_dup = _confirm(_near_duplicate(players), players)
    second = _correct(first, players, member)
    assert MatchFlag.objects.filter(match=second, duplicate_of=confirmed_dup).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    assert _status(confirmed_dup) == MatchStatus.VOIDED
    assert _status(second) == MatchStatus.CONFIRMED


def test_a_correction_of_a_match_recorded_after_the_other_is_still_the_newer_one(
    confirmed: Match, players: list[Player], event: Event, member: User
) -> None:
    later = _event_match(event, member, players, games=OTHER_GAMES)
    correction = _correct(later, players, member, games=((11, 2), (11, 3)))
    assert MatchFlag.objects.filter(match=correction, duplicate_of=confirmed).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    assert _status(correction) == MatchStatus.VOIDED
    assert _status(confirmed) == MatchStatus.CONFIRMED
    assert confirmed.pk in _counted()


def test_corrections_on_both_sides_compare_their_first_matches(
    organizer_confirmed: Match, players: list[Player], event: Event, member: User
) -> None:
    # Recorded in this order: the first match, second, then a correction
    # of each. The correction of ``second`` is the flag's ``match`` and
    # corrects a match recorded before the correction of the first, but
    # that correction keeps the place of the first.
    second = _event_match(event, member, players, games=OTHER_GAMES)
    first_fix = _correct(organizer_confirmed, players, member)
    second_fix = _correct(second, players, member, games=((11, 2), (11, 3)))
    assert MatchFlag.objects.filter(match=second_fix, duplicate_of=first_fix).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    assert _status(second_fix) == MatchStatus.VOIDED
    assert _status(first_fix) == MatchStatus.CONFIRMED
    assert first_fix.pk in _counted()


def test_a_pending_correction_of_an_older_match_voids_nothing(
    pending: Match, players: list[Player]
) -> None:
    confirmed_dup = _confirm(_near_duplicate(players), players)
    # A player's correction of their pending self-reported match (#267).
    correction = edit_match(
        pending,
        actor=players[0].account,
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        as_player=True,
    )
    assert correction.status == MatchStatus.PENDING
    assert MatchFlag.objects.filter(
        match=correction, duplicate_of=confirmed_dup
    ).exists()

    # The correction is the earlier match, and it is not confirmed.
    assert expire_disputes_and_holds(now=_days_from_now(60)) == 0

    assert _status(correction) == MatchStatus.PENDING
    assert _status(confirmed_dup) == MatchStatus.CONFIRMED
    assert _expire_events() == []


def test_the_recheck_under_the_locks_never_voids_the_earlier_match(
    organizer_confirmed: Match, players: list[Player], member: User
) -> None:
    confirmed_dup = _confirm(_near_duplicate(players), players)
    correction = _correct(organizer_confirmed, players, member)
    cutoff = _days_from_now(15) - timedelta(days=14)

    # The correction is the flag's ``match`` but the earlier match, so
    # the recheck refuses it even when asked for it directly.
    assert _expire_hold(correction.pk, cutoff) is False
    assert _status(correction) == MatchStatus.CONFIRMED
    assert _expire_hold(confirmed_dup.pk, cutoff) is True
    assert _status(confirmed_dup) == MatchStatus.VOIDED


# A correction keeps its original's place only while its chain keeps the
# same teams (in either order) and date (maintainer's decision on #268).
# Otherwise an Organizer could edit an old match onto another Organizer's
# confirmed result and take an earlier place than it.


def _organizer_match(
    event: Event,
    team_a: list[Player],
    team_b: list[Player],
    played_on: date,
    games: tuple[tuple[int, int], ...] = WORKFLOW_GAMES,
) -> tuple[Match, User]:
    member: User = OrganizerMemberFactory.create(organizer=event.organizer).account
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=team_a,
        team_b=team_b,
        games=games,
        played_on=played_on,
        event=event,
        submitted_by_user=member,
    ).match
    assert match.status == MatchStatus.CONFIRMED
    return match, member


def _organizer_edit(match: Match, member: User, players: list[Player]) -> Match:
    """Move ``match`` onto ``players``' lineup on WORKFLOW_PLAYED_ON."""
    return edit_match(
        match,
        actor=member,
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 1), (11, 0)),
        played_on=WORKFLOW_PLAYED_ON,
    )


@pytest.mark.parametrize("moved", ["teams", "date"])
def test_an_edit_onto_another_organizers_result_gets_no_old_place(
    players: list[Player], event: Event, moved: str
) -> None:
    other_event = EventFactory.create(organizer=verified_organizer())
    if moved == "teams":
        strangers = [PlayerFactory.create() for _ in range(4)]
        old_a, old_b, old_on = strangers[:2], strangers[2:], WORKFLOW_PLAYED_ON
    else:
        old_a, old_b = players[:2], players[2:]
        old_on = WORKFLOW_PLAYED_ON - timedelta(days=3)
    # Organizer X's old match, recorded first, with other teams or date.
    old, x_member = _organizer_match(other_event, old_a, old_b, old_on)
    # Organizer Y's real result on the lineup and date.
    real, _ = _organizer_match(event, players[:2], players[2:], WORKFLOW_PLAYED_ON)
    moved_match = _organizer_edit(old, x_member, players)
    assert moved_match.status == MatchStatus.CONFIRMED
    assert MatchFlag.objects.filter(match=moved_match, duplicate_of=real).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    assert _status(moved_match) == MatchStatus.VOIDED
    assert _status(real) == MatchStatus.CONFIRMED
    assert real.pk in _counted()


def test_a_correction_that_swaps_the_teams_keeps_its_place(
    organizer_confirmed: Match, players: list[Player], member: User
) -> None:
    confirmed_dup = _confirm(_near_duplicate(players), players)
    swapped = edit_match(
        organizer_confirmed,
        actor=member,
        format=MatchFormat.DOUBLES,
        team_a=players[2:],
        team_b=players[:2],
        games=((7, 11), (11, 9), (4, 11)),
        played_on=WORKFLOW_PLAYED_ON,
    )
    assert MatchFlag.objects.filter(match=swapped, duplicate_of=confirmed_dup).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    assert _status(confirmed_dup) == MatchStatus.VOIDED
    assert _status(swapped) == MatchStatus.CONFIRMED


def test_a_chain_that_leaves_and_returns_to_the_lineup_keeps_no_old_place(
    organizer_confirmed: Match, players: list[Player], member: User
) -> None:
    # first (lineup L) -> away (other date) -> back (lineup L again).
    away = edit_match(
        organizer_confirmed,
        actor=member,
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON - timedelta(days=1),
    )
    confirmed_dup = _confirm(_near_duplicate(players), players)
    back = _correct(away, players, member)
    assert MatchFlag.objects.filter(match=back, duplicate_of=confirmed_dup).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 1

    # The chain left the lineup, so ``back`` ranks by its own id.
    assert _status(back) == MatchStatus.VOIDED
    assert _status(confirmed_dup) == MatchStatus.CONFIRMED


def test_a_match_newer_in_one_flag_and_earlier_in_another(
    organizer_confirmed: Match, players: list[Player], member: User
) -> None:
    # Recorded in this order: the first match (A), middle (B), then a
    # confirmed correction of A (C, place A), then newest (D). B is newer
    # than C but earlier than D, and D is newer than both.
    middle = _confirm(_near_duplicate(players), players)
    correction = _correct(organizer_confirmed, players, member)
    newest = _confirm(
        record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=((11, 2), (11, 3)),
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=players[0].account,
        ).match,
        players,
    )
    open_links = set(
        MatchFlag.objects.filter(
            resolved_at__isnull=True,
            match__status=MatchStatus.CONFIRMED,
            duplicate_of__status=MatchStatus.CONFIRMED,
        ).values_list("match_id", "duplicate_of_id")
    )
    assert open_links == {
        (correction.pk, middle.pk),
        (newest.pk, middle.pk),
        (newest.pk, correction.pk),
    }

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 2

    assert _status(middle) == MatchStatus.VOIDED
    assert _status(newest) == MatchStatus.VOIDED
    assert _status(correction) == MatchStatus.CONFIRMED
    assert correction.pk in _counted()
    assert expire_disputes_and_holds(now=_days_from_now(16)) == 0


def test_a_match_only_on_the_duplicate_of_side_of_several_flags_is_voided_once(
    organizer_confirmed: Match, players: list[Player], member: User
) -> None:
    # B and C are both newer than A but held only against each other and
    # A; after A is corrected (D, place A), D is flagged against B and C,
    # so B and C are voided only through flags where they are
    # ``duplicate_of``.
    b = _confirm(_near_duplicate(players), players)
    c = _confirm(
        record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=((11, 2), (11, 3)),
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=players[0].account,
        ).match,
        players,
    )
    d = _correct(organizer_confirmed, players, member)
    assert MatchFlag.objects.filter(match=d, duplicate_of=b).exists()
    assert MatchFlag.objects.filter(match=d, duplicate_of=c).exists()

    assert expire_disputes_and_holds(now=_days_from_now(15)) == 2

    assert _status(b) == MatchStatus.VOIDED
    assert _status(c) == MatchStatus.VOIDED
    assert _status(d) == MatchStatus.CONFIRMED
    events = _expire_events()
    assert sorted(int(e.target_object_id) for e in events) == sorted([b.pk, c.pk])

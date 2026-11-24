"""Match type and confirmers for the engine (#44, ADR 0024).

A match with an Event is its kind; with a Club and no Event it is
``rec_verified``; with neither it is self-reported. A self-reported match
a moderator confirmed (a dispute ruled confirmed, or a moderator's
correction created confirmed) sends every participant as
``confirmed_by``, read from the audit log. Every other match sends only
the players who confirmed it. Each case is recorded through
``RatingEngine(load_config())``. Made-up data only.
"""

from datetime import date

import open_engine
import pytest
from django.contrib.auth.models import Permission
from pytest_django import DjangoAssertNumQueries

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Club
from apps.clubs.tests.factories import ClubFactory, ClubRoleFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.factories import raw_update
from apps.matches.workflow import (
    confirm_match,
    dispute_match,
    edit_match,
    resolve_dispute,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.engine_adapter import (
    counted_engine_matches,
    load_config,
    to_engine_match,
)

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 3, 1)
GAMES = [(11, 4)]
OTHER_GAMES = [(11, 7)]


@pytest.fixture
def players() -> list[Player]:
    """Team A is players 0 and 1, team B players 2 and 3."""
    return [PlayerFactory.create() for _ in range(4)]


@pytest.fixture
def moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


@pytest.fixture
def club() -> Club:
    made: Club = ClubFactory.create()
    return made


def _club_manager(club: Club) -> User:
    account: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=account)
    return account


def _codes(players: list[Player]) -> frozenset[str]:
    return frozenset(str(player.public_code) for player in players)


def _submit(
    players: list[Player],
    *,
    games: list[tuple[int, int]] = GAMES,
    club: Club | None = None,
    submitter: User | None = None,
) -> Match:
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=PLAYED_ON,
        submitted_by_user=submitter or players[0].account,
        club=club,
    ).match


def _edit(match: Match, players: list[Player], moderator: User) -> Match:
    return edit_match(
        match,
        actor=moderator,
        reason="Corrected the score.",
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=OTHER_GAMES,
        played_on=PLAYED_ON,
    )


def _fresh(match: Match) -> Match:
    return Match.objects.get(pk=match.pk)


def _record(match: Match) -> open_engine.MatchResult:
    return open_engine.RatingEngine(load_config()).record(to_engine_match(match))


def _listed(match: Match) -> open_engine.Match:
    [listed] = [m for m in counted_engine_matches() if m.match_id == match.public_code]
    return listed


# Match type.


def test_a_club_match_with_no_event_is_rec_verified(
    players: list[Player], club: Club
) -> None:
    match = _submit(players, club=club, submitter=_club_manager(club))

    converted = to_engine_match(_fresh(match))

    assert match.status == MatchStatus.CONFIRMED
    assert converted.match_type is open_engine.MatchType.REC_VERIFIED
    assert converted.confirmed_by == frozenset()
    assert _record(match).counted
    assert _listed(match) == converted


def test_a_match_with_no_event_and_no_club_is_self_reported(
    players: list[Player],
) -> None:
    match = _submit(players)

    assert to_engine_match(match).match_type is open_engine.MatchType.SELF_REPORTED


# Confirmers.


def test_both_teams_confirming_sends_only_the_confirmers(
    players: list[Player],
) -> None:
    match = _submit(players)
    confirm_match(match, actor=players[1].account)
    confirm_match(match, actor=players[2].account)

    converted = to_engine_match(_fresh(match))

    assert converted.confirmed_by == _codes([players[1], players[2]])
    assert _record(match).counted
    assert _listed(match) == converted


def test_one_team_confirming_never_passes(players: list[Player]) -> None:
    match = _submit(players)
    confirm_match(match, actor=players[0].account)
    assert _fresh(match).status == MatchStatus.PENDING
    assert counted_engine_matches() == []

    # Even marked confirmed by a raw write, one team's confirmation is not enough.
    raw_update(match, status=MatchStatus.CONFIRMED)
    converted = to_engine_match(_fresh(match))

    assert converted.confirmed_by == _codes([players[0]])
    result = _record(match)
    assert not result.counted
    assert result.reason is open_engine.RejectionReason.SELF_REPORTED_UNCONFIRMED


def test_a_dispute_ruled_confirmed_sends_every_participant(
    players: list[Player], moderator: User
) -> None:
    match = _submit(players)
    confirm_match(match, actor=players[0].account)
    dispute_match(match, actor=players[2].account)
    resolve_dispute(
        match, actor=moderator, outcome=MatchStatus.CONFIRMED, reason="Checked it."
    )

    converted = to_engine_match(_fresh(match))

    assert converted.match_type is open_engine.MatchType.SELF_REPORTED
    assert converted.confirmed_by == _codes(players)
    assert _record(match).counted
    assert _listed(match) == converted


def test_a_moderator_correction_of_a_confirmed_match_sends_every_participant(
    players: list[Player], moderator: User
) -> None:
    old = _submit(players)
    confirm_match(old, actor=players[0].account)
    confirm_match(old, actor=players[3].account)

    corrected = _edit(_fresh(old), players, moderator)

    assert corrected.status == MatchStatus.CONFIRMED
    converted = to_engine_match(_fresh(corrected))
    assert converted.confirmed_by == _codes(players)
    assert _record(corrected).counted
    assert _listed(corrected) == converted


def test_a_moderator_correction_of_a_disputed_match_sends_every_participant(
    players: list[Player], moderator: User
) -> None:
    old = _submit(players)
    dispute_match(old, actor=players[1].account)

    corrected = _edit(_fresh(old), players, moderator)

    assert corrected.status == MatchStatus.CONFIRMED
    assert to_engine_match(_fresh(corrected)).confirmed_by == _codes(players)
    assert _record(corrected).counted


def test_a_pending_correction_confirmed_by_both_teams_sends_the_confirmers(
    players: list[Player], moderator: User
) -> None:
    old = _submit(players)
    corrected = _edit(old, players, moderator)
    assert corrected.status == MatchStatus.PENDING
    confirm_match(corrected, actor=players[0].account)
    confirm_match(corrected, actor=players[3].account)

    converted = to_engine_match(_fresh(corrected))

    assert _fresh(corrected).status == MatchStatus.CONFIRMED
    assert converted.confirmed_by == _codes([players[0], players[3]])
    assert _record(corrected).counted
    assert _listed(corrected) == converted


def test_a_club_duplicate_of_a_pending_self_submission_keeps_its_confirmers(
    players: list[Player], club: Club
) -> None:
    pending = _submit(players)
    confirm_match(pending, actor=players[0].account)

    recorded = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=_club_manager(club),
        club=club,
    )

    assert not recorded.created
    assert recorded.match.pk == pending.pk
    match = _fresh(pending)
    assert match.status == MatchStatus.CONFIRMED
    assert match.club_id is None
    converted = to_engine_match(match)
    assert converted.match_type is open_engine.MatchType.SELF_REPORTED
    assert converted.confirmed_by == _codes([players[0]])
    result = _record(match)
    assert not result.counted
    assert result.reason is open_engine.RejectionReason.SELF_REPORTED_UNCONFIRMED
    assert _listed(match) == converted


def test_a_club_dispute_ruled_confirmed_sends_only_the_confirmers(
    players: list[Player], club: Club, moderator: User
) -> None:
    match = _submit(players, club=club, submitter=_club_manager(club))
    dispute_match(match, actor=players[2].account)
    resolve_dispute(
        match, actor=moderator, outcome=MatchStatus.CONFIRMED, reason="Checked it."
    )

    converted = to_engine_match(_fresh(match))

    assert converted.match_type is open_engine.MatchType.REC_VERIFIED
    assert converted.confirmed_by == frozenset()
    assert _record(match).counted


def test_another_match_s_ruling_does_not_count_for_this_one(
    players: list[Player], moderator: User
) -> None:
    ruled = _submit(players)
    dispute_match(ruled, actor=players[2].account)
    resolve_dispute(
        ruled, actor=moderator, outcome=MatchStatus.CONFIRMED, reason="Checked it."
    )
    other = _submit(players, games=OTHER_GAMES)
    confirm_match(other, actor=players[0].account)
    raw_update(other, status=MatchStatus.CONFIRMED)

    assert to_engine_match(_fresh(other)).confirmed_by == _codes([players[0]])


def test_counted_engine_matches_reads_moderator_actions_in_fixed_queries(
    players: list[Player],
    moderator: User,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    ruled = _submit(players)
    dispute_match(ruled, actor=players[2].account)
    resolve_dispute(
        ruled, actor=moderator, outcome=MatchStatus.CONFIRMED, reason="Checked it."
    )
    others = [PlayerFactory.create() for _ in range(4)]
    old = _submit(others)
    confirm_match(old, actor=others[0].account)
    confirm_match(old, actor=others[2].account)
    corrected = _edit(_fresh(old), others, moderator)
    third = [PlayerFactory.create() for _ in range(4)]
    plain = _submit(third)
    confirm_match(plain, actor=third[1].account)
    confirm_match(plain, actor=third[3].account)

    with django_assert_num_queries(4):
        listed = {m.match_id: m for m in counted_engine_matches()}

    assert listed[ruled.public_code].confirmed_by == _codes(players)
    assert listed[corrected.public_code].confirmed_by == _codes(others)
    assert listed[plain.public_code].confirmed_by == _codes([third[1], third[3]])
    for match in (ruled, corrected, plain):
        assert listed[match.public_code] == to_engine_match(_fresh(match))

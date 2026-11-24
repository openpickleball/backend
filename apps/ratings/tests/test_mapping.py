"""Backend Matches convert to the engine's Match (#44).

Every value goes through an explicit table, and a value a table does not
know raises ``EngineMappingError`` naming the field. The match type comes
only from the Event, never from what a submitter sent. Made-up data only.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import open_engine
import pytest
from django.db.models import Prefetch

from apps.clubs.tests.factories import ClubFactory
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    BestOf,
    GameTarget,
    Match,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
    ScoringType,
    Team,
)
from apps.players.models import GenderDivision, Player
from apps.ratings.engine_adapter import EngineMappingError, to_engine_match
from apps.ratings.tests.builders import MIXED, PLAYED_ON, THREE_GAMES, saved_match

pytestmark = pytest.mark.django_db

MENS = GenderDivision.MENS
WOMENS = GenderDivision.WOMENS


def _codes(players: list[Player]) -> tuple[str, ...]:
    return tuple(sorted(str(p.public_code) for p in players))


def _prefetched(match: Match) -> Match:
    """The Match fetched with its rows cached, so a test can change them in memory."""
    return (
        Match.objects.select_related("event")
        .prefetch_related(
            Prefetch(
                "participants",
                queryset=MatchParticipant.objects.select_related("player"),
            ),
            "games",
            "confirmations",
        )
        .get(pk=match.pk)
    )


def test_a_confirmed_doubles_match_converts_field_by_field() -> None:
    event = EventFactory.create(kind=EventKind.TOURNAMENT)
    match, players = saved_match(
        event=event,
        confirm=[(Team.A, 1), (Team.B, 0)],
        dispute=[(Team.B, 1)],
        sequence=3,
    )

    converted = to_engine_match(match)

    assert type(converted) is open_engine.Match
    assert converted.match_id == str(match.public_code)
    assert type(converted.match_id) is str
    assert converted.team_a == _codes(players[Team.A])
    assert converted.team_b == _codes(players[Team.B])
    assert type(converted.team_a) is tuple
    assert all(type(code) is str for code in converted.team_a + converted.team_b)
    assert converted.games == THREE_GAMES
    assert all(type(s) is int for game in converted.games for s in game)
    assert converted.played_on == PLAYED_ON
    assert type(converted.played_on) is date
    assert converted.rating_format is open_engine.RatingFormat.MIXED_DOUBLES
    assert converted.match_type is open_engine.MatchType.TOURNAMENT
    assert converted.best_of == 3
    assert type(converted.best_of) is int
    assert converted.game_format == open_engine.GameFormat(
        scoring=open_engine.ScoringType.SIDE_OUT, points_to=11, win_by=2, cap=None
    )
    assert converted.played_at is None
    assert converted.sequence == 3
    assert type(converted.sequence) is int
    assert converted.confirmed_by == frozenset(
        {str(players[Team.A][1].public_code), str(players[Team.B][0].public_code)}
    )
    assert type(converted.confirmed_by) is frozenset
    assert converted.status is open_engine.MatchStatus.COMPLETED
    converted.validate()


def test_the_engine_records_a_converted_match() -> None:
    match, _ = saved_match(event=EventFactory.create(kind=EventKind.LEAGUE))

    result = open_engine.RatingEngine().record(to_engine_match(match))

    assert result.counted
    assert result.rating_format is open_engine.RatingFormat.MIXED_DOUBLES


def test_games_follow_game_number_and_team_sides() -> None:
    match, _ = saved_match(games=((5, 11), (11, 13), (11, 1)), best_of=BestOf.THREE)

    assert to_engine_match(match).games == ((5, 11), (11, 13), (11, 1))


@pytest.mark.parametrize(
    ("match_format", "divisions", "expected"),
    [
        (
            MatchFormat.SINGLES,
            {Team.A: (MENS,), Team.B: (WOMENS,)},
            open_engine.RatingFormat.SINGLES,
        ),
        (
            MatchFormat.DOUBLES,
            {Team.A: (MENS, MENS), Team.B: (MENS, MENS)},
            open_engine.RatingFormat.DOUBLES_MEN,
        ),
        (
            MatchFormat.DOUBLES,
            {Team.A: (WOMENS, WOMENS), Team.B: (WOMENS, WOMENS)},
            open_engine.RatingFormat.DOUBLES_WOMEN,
        ),
        (MatchFormat.DOUBLES, MIXED, open_engine.RatingFormat.MIXED_DOUBLES),
        (
            MatchFormat.DOUBLES,
            {Team.A: (MENS, MENS), Team.B: (WOMENS, WOMENS)},
            open_engine.RatingFormat.DOUBLES,
        ),
        (
            MatchFormat.DOUBLES,
            {Team.A: (MENS, WOMENS), Team.B: (MENS, MENS)},
            open_engine.RatingFormat.DOUBLES,
        ),
        (
            MatchFormat.DOUBLES,
            {Team.A: (MENS, WOMENS), Team.B: (WOMENS, WOMENS)},
            open_engine.RatingFormat.DOUBLES,
        ),
        (
            MatchFormat.DOUBLES,
            {Team.A: (MENS, MENS), Team.B: (MENS, "")},
            open_engine.RatingFormat.DOUBLES,
        ),
        (
            MatchFormat.DOUBLES,
            {Team.A: (MENS, ""), Team.B: (WOMENS, MENS)},
            open_engine.RatingFormat.DOUBLES,
        ),
        (
            MatchFormat.SINGLES,
            {Team.A: ("",), Team.B: (WOMENS,)},
            open_engine.RatingFormat.SINGLES,
        ),
    ],
)
def test_rating_format_follows_the_format_and_divisions(
    match_format: str,
    divisions: dict[Team, tuple[str, ...]],
    expected: open_engine.RatingFormat,
) -> None:
    match, _ = saved_match(
        match_format=match_format,
        divisions=divisions,
        games=((11, 4),),
        best_of=BestOf.ONE,
    )

    assert to_engine_match(match).rating_format is expected


@pytest.mark.parametrize("kind", EventKind.values)
def test_each_event_kind_is_the_engine_match_type_of_the_same_value(kind: str) -> None:
    match, _ = saved_match(event=EventFactory.create(kind=kind))

    converted = to_engine_match(match).match_type

    assert converted is open_engine.MatchType(kind)
    assert converted is not open_engine.MatchType.SELF_REPORTED


def test_a_match_with_no_event_is_self_reported() -> None:
    match, _ = saved_match()

    assert to_engine_match(match).match_type is open_engine.MatchType.SELF_REPORTED


def test_a_club_match_with_no_event_is_self_reported() -> None:
    match, _ = saved_match(club=ClubFactory.create())

    assert to_engine_match(match).match_type is open_engine.MatchType.SELF_REPORTED


@pytest.mark.parametrize("scoring", ScoringType.values)
@pytest.mark.parametrize("target", GameTarget.values)
def test_game_format_follows_scoring_and_target(scoring: str, target: int) -> None:
    match, _ = saved_match(scoring_type=scoring, game_target=target)

    assert to_engine_match(match).game_format == open_engine.GameFormat(
        scoring=open_engine.ScoringType(scoring), points_to=target, win_by=2, cap=None
    )
    assert type(to_engine_match(match).game_format.points_to) is int


@pytest.mark.parametrize("best_of", BestOf.values)
def test_best_of_is_a_plain_int(best_of: int) -> None:
    match, _ = saved_match(best_of=best_of)

    converted = to_engine_match(match).best_of

    assert converted == best_of
    assert type(converted) is int


def test_confirmed_by_holds_only_confirmations() -> None:
    match, players = saved_match(confirm=[(Team.B, 1)], dispute=[(Team.A, 0)])

    assert to_engine_match(match).confirmed_by == frozenset(
        {str(players[Team.B][1].public_code)}
    )


def test_no_confirmations_give_an_empty_frozenset() -> None:
    match, _ = saved_match(event=EventFactory.create())

    converted = to_engine_match(match).confirmed_by

    assert converted == frozenset()
    assert type(converted) is frozenset


def test_played_at_converts_to_the_local_offset() -> None:
    # 01:30 UTC on 1 March is 20:30 on 28 February at UTC-5.
    instant = datetime(2026, 3, 1, 1, 30, tzinfo=UTC)
    match, _ = saved_match(
        played_on=date(2026, 2, 28), played_at=instant, played_at_utc_offset=-300
    )

    played_at = to_engine_match(match).played_at

    assert played_at is not None
    assert type(played_at) is datetime
    assert type(played_at.tzinfo) is timezone
    assert played_at.utcoffset() == timedelta(minutes=-300)
    assert played_at == instant
    assert played_at.date() == date(2026, 2, 28)
    assert (played_at.hour, played_at.minute) == (20, 30)
    to_engine_match(match).validate()


def test_played_at_at_utc_keeps_a_zero_offset() -> None:
    instant = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    match, _ = saved_match(played_at=instant, played_at_utc_offset=0)

    played_at = to_engine_match(match).played_at

    assert played_at == instant
    assert played_at is not None
    assert type(played_at.tzinfo) is timezone
    assert played_at.utcoffset() == timedelta(0)


def test_a_converted_match_passes_the_engine_checks_in_every_format() -> None:
    for divisions in (
        MIXED,
        {Team.A: (MENS, MENS), Team.B: (MENS, MENS)},
        {Team.A: (WOMENS, WOMENS), Team.B: (WOMENS, WOMENS)},
        {Team.A: (MENS, MENS), Team.B: (WOMENS, WOMENS)},
    ):
        match, _ = saved_match(divisions=divisions)
        to_engine_match(match).validate()


# Unknown values. Each is set in memory, since the database refuses them.


def _expect_mapping_error(match: Match, *words: str) -> str:
    with pytest.raises(EngineMappingError) as raised:
        to_engine_match(match)
    message = str(raised.value)
    for word in words:
        assert word in message
    return message


def test_an_unknown_format_raises() -> None:
    match = _prefetched(saved_match()[0])
    match.format = "triples"

    _expect_mapping_error(match, "format", str(match.public_code))


def test_an_unknown_event_kind_raises() -> None:
    match = _prefetched(saved_match(event=EventFactory.create())[0])
    assert match.event is not None
    match.event.kind = "exhibition"

    _expect_mapping_error(match, "event kind")


def test_an_unknown_scoring_type_raises() -> None:
    match = _prefetched(saved_match()[0])
    match.scoring_type = "golden_point"

    _expect_mapping_error(match, "scoring type")


def test_an_unknown_game_target_raises() -> None:
    match = _prefetched(saved_match()[0])
    match.game_target = 13

    _expect_mapping_error(match, "game target")


def test_an_unknown_best_of_raises() -> None:
    match = _prefetched(saved_match()[0])
    match.best_of = 7

    _expect_mapping_error(match, "best of")


def test_an_unknown_team_raises() -> None:
    match = _prefetched(saved_match()[0])
    match.participants.all()[0].team = "C"

    _expect_mapping_error(match, "team")


def test_an_unknown_gender_division_raises_without_its_value() -> None:
    match = _prefetched(saved_match()[0])
    match.participants.all()[0].player.gender_division = "made_up_division"

    message = _expect_mapping_error(match, "gender division")

    assert "made_up_division" not in message


def test_an_unknown_gender_division_raises_in_singles_too() -> None:
    match = _prefetched(
        saved_match(match_format=MatchFormat.SINGLES, games=((11, 3),), best_of=1)[0]
    )
    match.participants.all()[0].player.gender_division = "made_up_division"

    message = _expect_mapping_error(match, "gender division")

    assert "made_up_division" not in message


def test_played_at_without_an_offset_raises() -> None:
    match = _prefetched(saved_match()[0])
    match.played_at = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

    _expect_mapping_error(match, "played_at", "offset")


def test_an_offset_without_played_at_raises() -> None:
    match = _prefetched(saved_match()[0])
    match.played_at_utc_offset = 60

    _expect_mapping_error(match, "played_at", "offset")


def test_a_naive_played_at_raises() -> None:
    match = _prefetched(saved_match()[0])
    match.played_at = datetime(2026, 3, 1, 9, 0)
    match.played_at_utc_offset = 0

    _expect_mapping_error(match, "played_at", "time zone")


def test_a_confirmation_by_a_non_participant_raises() -> None:
    match = _prefetched(saved_match(confirm=[(Team.A, 0)])[0])
    other = _prefetched(saved_match()[0])
    match.confirmations.all()[0].player_id = other.participants.all()[0].player_id

    _expect_mapping_error(match, "confirm")


def test_the_mapping_error_is_a_value_error() -> None:
    assert issubclass(EngineMappingError, ValueError)


def test_status_is_not_read_from_the_backend() -> None:
    # to_engine_match does not choose which matches count (counted() does).
    match, _ = saved_match(status=MatchStatus.PENDING)

    assert to_engine_match(match).status is open_engine.MatchStatus.COMPLETED

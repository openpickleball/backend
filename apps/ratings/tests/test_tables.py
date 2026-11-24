"""The adapter's tables cover exactly the backend's values (#44, ADR 0024).

A new backend choice with no table entry would raise; a table entry with
no backend choice would be dead. Each lookup takes only the exact type
of its key, so a bool or a float never passes as a number. Made-up data
only.
"""

import open_engine
import open_engine.engine
import pytest

from apps.events.models import EventKind
from apps.matches.models import (
    BestOf,
    GameTarget,
    Match,
    MatchFormat,
    RatingFormat,
    ScoringType,
    Team,
)
from apps.matches.tests.factories import DoublesMatchFactory, MatchFactory
from apps.ratings import engine_adapter
from apps.ratings.engine_adapter import EngineMappingError, to_engine_match


def test_the_match_format_table() -> None:
    assert set(engine_adapter._MATCH_FORMATS) == set(MatchFormat.values)
    assert set(engine_adapter._MATCH_FORMATS.values()) == {
        open_engine.RatingFormat.SINGLES,
        open_engine.RatingFormat.DOUBLES,
    }


def test_the_rating_format_table() -> None:
    table = engine_adapter._RATING_FORMATS
    assert set(table) == set(RatingFormat.values)
    assert set(table.values()) == set(open_engine.RatingFormat)
    assert all(table[key].value == key for key in table)


def test_the_match_type_table() -> None:
    table = engine_adapter._MATCH_TYPES
    assert set(table) == set(EventKind.values)
    assert set(table.values()) == set(open_engine.MatchType) - {
        open_engine.MatchType.SELF_REPORTED
    }
    assert all(table[key].value == key for key in table)


def test_the_scoring_type_table() -> None:
    table = engine_adapter._SCORING_TYPES
    assert set(table) == set(ScoringType.values)
    assert set(table.values()) == set(open_engine.ScoringType)
    assert all(table[key].value == key for key in table)


def test_the_game_target_table() -> None:
    table = engine_adapter._GAME_TARGETS
    assert set(table) == set(GameTarget.values)
    assert set(table.values()) == {
        points for _, points in open_engine.engine.ALL_GAME_FORMATS
    }
    assert all(table[key] == key for key in table)


def test_the_best_of_table() -> None:
    table = engine_adapter._BEST_OF
    assert set(table) == set(BestOf.values)
    assert all(table[key] == key for key in table)


def test_the_team_table() -> None:
    assert set(engine_adapter._TEAMS) == set(Team.values)


def test_every_table_key_is_a_plain_value() -> None:
    for table in (
        engine_adapter._MATCH_FORMATS,
        engine_adapter._RATING_FORMATS,
        engine_adapter._MATCH_TYPES,
        engine_adapter._SCORING_TYPES,
        engine_adapter._TEAMS,
    ):
        assert all(type(key) is str for key in table)
    for numbers in (engine_adapter._GAME_TARGETS, engine_adapter._BEST_OF):
        assert all(type(key) is int for key in numbers)
        assert all(type(value) is int for value in numbers.values())


# Exact key types.


@pytest.fixture
def match(db: None) -> Match:
    made: Match = MatchFactory.create(best_of_3=True)
    return Match.objects.get(pk=made.pk)


@pytest.mark.parametrize(
    ("field", "value", "words"),
    [
        ("best_of", True, "best of"),
        ("best_of", 3.0, "best of"),
        ("game_target", 11.0, "game target"),
        ("format", 1, "format"),
        ("scoring_type", 1.5, "scoring type"),
    ],
)
def test_a_lookup_refuses_a_value_of_the_wrong_type(
    match: Match, field: str, value: object, words: str
) -> None:
    setattr(match, field, value)

    with pytest.raises(EngineMappingError, match=words):
        to_engine_match(match)


def test_a_lookup_takes_a_choices_member(match: Match) -> None:
    match.best_of = BestOf.THREE
    match.game_target = GameTarget.FIFTEEN
    match.scoring_type = ScoringType.RALLY

    converted = to_engine_match(match)

    assert type(converted.best_of) is int
    assert converted.best_of == 3
    assert type(converted.game_format.points_to) is int
    assert converted.game_format.points_to == 15
    assert converted.game_format.scoring is open_engine.ScoringType.RALLY


@pytest.mark.django_db
def test_an_unsaved_match_is_refused() -> None:
    unsaved = DoublesMatchFactory.build()

    with pytest.raises(EngineMappingError, match="saved"):
        to_engine_match(unsaved)

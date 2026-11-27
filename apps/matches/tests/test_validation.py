"""Score validation: the rules ``validate_match`` applies to every submission.

These tests need no database: the function takes plain values and players
as any hashable reference (made-up strings here).
"""

import inspect
import re
from collections.abc import Hashable, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any

import pytest
from django.utils import timezone

from apps.matches.models import (
    MAX_GAMES,
    MAX_UTC_OFFSET_MINUTES,
    BestOf,
    GameTarget,
    Match,
    MatchFormat,
    ScoringType,
    Team,
)
from apps.matches.services import (
    MatchIssue,
    MatchValidationError,
    ValidatedMatch,
    infer_best_of,
    validate_match,
)
from apps.matches.tests.factories import DoublesMatchFactory, MatchFactory

# Numbers a message may contain: game numbers, the win margin, and the
# allowed best-of and target values. A submitted score outside this set
# showing up in a message would be a leak.
_SAFE_NUMBERS = {1, 2, 3, 4, 5, *BestOf.values, *GameTarget.values}

_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
_REAL_NOW = timezone.now
_PAST = date(2026, 6, 1)

SINGLES_A = ("player-a1",)
SINGLES_B = ("player-b1",)
DOUBLES_A = ("player-a1", "player-a2")
DOUBLES_B = ("player-b1", "player-b2")


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apps.matches.services.timezone.now", lambda: _NOW)


def _validate(**overrides: Any) -> ValidatedMatch:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": SINGLES_A,
        "team_b": SINGLES_B,
        "games": [(11, 5)],
        "played_on": _PAST,
    }
    values.update(overrides)
    return validate_match(**values)


def _issues(**overrides: Any) -> tuple[MatchIssue, ...]:
    with pytest.raises(MatchValidationError) as caught:
        _validate(**overrides)
    issues = caught.value.issues
    assert issues
    for issue in issues:
        _assert_no_leaked_number(issue.message)
    return issues


def _codes(**overrides: Any) -> set[tuple[str, str]]:
    return {(issue.field, issue.code) for issue in _issues(**overrides)}


def _assert_no_leaked_number(message: str) -> None:
    numbers = {int(found) for found in re.findall(r"\d+", message)}
    assert numbers <= _SAFE_NUMBERS, message


def _outcome(**overrides: Any) -> set[tuple[str, str]] | None:
    """None when the match passes, else its (field, code) pairs."""
    try:
        _validate(**overrides)
    except MatchValidationError as error:
        return {(issue.field, issue.code) for issue in error.issues}
    return None


# Criteria 1 and 7: single games at each target, best of 1.
GAME_TABLE: list[tuple[int, tuple[int, int], str | None]] = [
    (11, (11, 9), None),
    (11, (9, 11), None),
    (11, (11, 0), None),
    (11, (13, 11), None),
    (11, (11, 10), "not_won_by_two"),
    (11, (12, 9), "played_past_target"),
    (11, (14, 11), "played_past_target"),
    (11, (10, 8), "target_not_reached"),
    (11, (11, 11), "tie"),
    (11, (0, 0), "tie"),
    (15, (15, 13), None),
    (15, (17, 15), None),
    (15, (15, 14), "not_won_by_two"),
    (15, (14, 12), "target_not_reached"),
    (15, (18, 15), "played_past_target"),
    (21, (21, 19), None),
    (21, (23, 21), None),
    (21, (21, 20), "not_won_by_two"),
    (21, (20, 18), "target_not_reached"),
    (21, (24, 21), "played_past_target"),
]


@pytest.mark.parametrize("scoring_type", ScoringType.values)
@pytest.mark.parametrize(("target", "score", "code"), GAME_TABLE)
def test_game_scores(
    scoring_type: str, target: int, score: tuple[int, int], code: str | None
) -> None:
    overrides: dict[str, Any] = {
        "games": [score],
        "best_of": 1,
        "game_target": target,
        "scoring_type": scoring_type,
    }
    if code is None:
        result = _validate(**overrides)
        assert result.games == (score,)
    else:
        issues = _issues(**overrides)
        assert {(issue.field, issue.code) for issue in issues} == {("games.0", code)}
        assert all("Game 1" in issue.message for issue in issues)
        assert f"{score[0]}-{score[1]}" not in issues[0].message


# Criterion 5: the same scores give the same outcome under both scoring types.
@pytest.mark.parametrize(("target", "score", "code"), GAME_TABLE)
def test_scoring_type_does_not_change_the_game_rules(
    target: int, score: tuple[int, int], code: str | None
) -> None:
    outcomes = [
        _outcome(games=[score], best_of=1, game_target=target, scoring_type=kind)
        for kind in ScoringType.values
    ]
    assert outcomes[0] == outcomes[1]


# Criterion 2: teams.
@pytest.mark.parametrize(
    ("match_format", "team_a", "team_b", "expected"),
    [
        (MatchFormat.SINGLES, SINGLES_A, SINGLES_B, None),
        (MatchFormat.DOUBLES, DOUBLES_A, DOUBLES_B, None),
        (MatchFormat.SINGLES, DOUBLES_A, SINGLES_B, {("team_a", "team_size")}),
        (MatchFormat.SINGLES, SINGLES_A, DOUBLES_B, {("team_b", "team_size")}),
        (MatchFormat.SINGLES, (), SINGLES_B, {("team_a", "team_size")}),
        (MatchFormat.DOUBLES, SINGLES_A, DOUBLES_B, {("team_a", "team_size")}),
        (
            MatchFormat.DOUBLES,
            DOUBLES_A,
            ("player-b1", "player-b2", "player-b3"),
            {("team_b", "team_size")},
        ),
        (
            MatchFormat.DOUBLES,
            ("player-a1", "player-a1"),
            DOUBLES_B,
            {("team_a", "duplicate_player")},
        ),
        (
            MatchFormat.DOUBLES,
            DOUBLES_A,
            ("player-b1", "player-b1"),
            {("team_b", "duplicate_player")},
        ),
        (
            MatchFormat.DOUBLES,
            DOUBLES_A,
            ("player-a2", "player-b2"),
            {("team_b", "player_on_both_teams")},
        ),
        (
            MatchFormat.SINGLES,
            SINGLES_A,
            SINGLES_A,
            {("team_b", "player_on_both_teams")},
        ),
    ],
)
def test_teams(
    match_format: str,
    team_a: Sequence[Hashable],
    team_b: Sequence[Hashable],
    expected: set[tuple[str, str]] | None,
) -> None:
    overrides = {"format": match_format, "team_a": team_a, "team_b": team_b}
    if expected is None:
        result = _validate(**overrides)
        assert result.format == match_format
        assert result.team_a == tuple(team_a)
        assert result.team_b == tuple(team_b)
    else:
        assert _codes(**overrides) == expected


def test_an_unknown_format_fails_and_skips_the_team_size_check() -> None:
    issues = _issues(format="triples", team_a=DOUBLES_A, team_b=SINGLES_B)
    assert [(issue.field, issue.code) for issue in issues] == [
        ("format", "invalid_choice")
    ]
    assert "singles" in issues[0].message
    assert "doubles" in issues[0].message
    assert "triples" not in issues[0].message


# Criterion 3: best-of values.
@pytest.mark.parametrize(
    ("best_of", "games"),
    [
        (1, [(11, 5)]),
        (3, [(11, 5), (11, 7)]),
        (5, [(11, 5), (11, 7), (11, 3)]),
    ],
)
def test_allowed_best_of_values_pass(
    best_of: int, games: list[tuple[int, int]]
) -> None:
    result = _validate(best_of=best_of, games=games)
    assert result.best_of == best_of
    assert isinstance(result.best_of, BestOf)


@pytest.mark.parametrize("best_of", [0, 2, 4, 7, True, "3", 3.0, -1])
def test_other_best_of_values_fail(best_of: object) -> None:
    issues = _issues(best_of=best_of)
    assert [(issue.field, issue.code) for issue in issues] == [
        ("best_of", "invalid_choice")
    ]
    assert issues[0].message == "Choose a best-of of 1, 3, or 5."


# Criterion 4: games that decide the match.
def test_best_of_3_with_one_game_is_not_decided() -> None:
    assert _codes(best_of=3, games=[(11, 5)]) == {("games", "not_decided")}


def test_best_of_3_with_a_game_after_the_match_was_decided_fails() -> None:
    issues = _issues(best_of=3, games=[(11, 5), (11, 7), (11, 3)])
    assert [(issue.field, issue.code) for issue in issues] == [
        ("games.2", "game_after_decided")
    ]
    assert "Game 3" in issues[0].message


def test_only_the_first_game_after_the_decision_is_reported() -> None:
    issues = _issues(best_of=1, games=[(11, 5), (11, 7), (11, 3)])
    assert [(issue.field, issue.code) for issue in issues] == [
        ("games.1", "game_after_decided")
    ]


def test_best_of_5_decided_in_game_4_passes() -> None:
    games = [(11, 5), (9, 11), (11, 7), (11, 6)]
    result = _validate(best_of=5, games=games)
    assert result.games == tuple(games)
    assert result.best_of == BestOf.FIVE


def test_best_of_5_won_by_team_b_passes() -> None:
    games = [
        (5, 11),
        (11, 9),
        (7, 11),
        (11, 13),
    ]
    assert _validate(best_of=5, games=games).best_of == BestOf.FIVE


# Criterion 6: game targets.
@pytest.mark.parametrize("scoring_type", ScoringType.values)
@pytest.mark.parametrize("target", GameTarget.values)
def test_allowed_game_targets_pass(scoring_type: str, target: int) -> None:
    result = _validate(
        games=[(target, 3)], game_target=target, scoring_type=scoring_type
    )
    assert result.game_target == target
    assert isinstance(result.game_target, GameTarget)
    assert result.scoring_type == scoring_type
    assert isinstance(result.scoring_type, ScoringType)


@pytest.mark.parametrize("scoring_type", ScoringType.values)
@pytest.mark.parametrize("target", [9, 25, 0, True, "11", 11.0])
def test_other_game_targets_fail(scoring_type: str, target: object) -> None:
    issues = _issues(game_target=target, scoring_type=scoring_type)
    assert [(issue.field, issue.code) for issue in issues] == [
        ("game_target", "invalid_choice")
    ]
    message = issues[0].message
    assert message == "Choose a game target of 11, 15, or 21."
    for allowed in ("11", "15", "21"):
        assert allowed in message


def test_an_invalid_target_skips_the_target_rules_but_keeps_the_others() -> None:
    codes = _codes(game_target=25, games=[(11, 11)])
    assert codes == {("game_target", "invalid_choice"), ("games.0", "tie")}


@pytest.mark.parametrize("scoring_type", ["", "Rally", "points", 1])
def test_unknown_scoring_types_fail(scoring_type: object) -> None:
    issues = _issues(scoring_type=scoring_type)
    assert [(issue.field, issue.code) for issue in issues] == [
        ("scoring_type", "invalid_choice")
    ]
    assert "side-out" in issues[0].message
    assert "rally" in issues[0].message
    assert "points" not in issues[0].message


# Criterion 8: defaults and inference.
def test_defaults_are_target_11_and_side_out() -> None:
    result = _validate()
    assert result == ValidatedMatch(
        format=MatchFormat.SINGLES,
        team_a=SINGLES_A,
        team_b=SINGLES_B,
        games=((11, 5),),
        played_on=_PAST,
        best_of=BestOf.ONE,
        game_target=GameTarget.ELEVEN,
        scoring_type=ScoringType.SIDE_OUT,
    )


def test_a_missing_target_means_11() -> None:
    assert _codes(games=[(15, 3)]) == {("games.0", "played_past_target")}


@pytest.mark.parametrize(
    ("games", "expected"),
    [
        ([(11, 5)], BestOf.ONE),
        ([(5, 11)], BestOf.ONE),
        ([(11, 5), (11, 7)], BestOf.THREE),
        ([(11, 5), (5, 11), (11, 3)], BestOf.THREE),
        ([(11, 5), (11, 7), (11, 3)], BestOf.FIVE),
        ([(11, 5), (5, 11), (5, 11), (11, 3), (11, 2)], BestOf.FIVE),
    ],
)
def test_best_of_is_inferred_when_absent(
    games: list[tuple[int, int]], expected: BestOf
) -> None:
    assert _validate(games=games).best_of == expected


@pytest.mark.parametrize(
    "games",
    [
        [(11, 5), (5, 11)],
        [(11, 5), (5, 11), (11, 3), (3, 11)],
    ],
)
def test_best_of_cannot_be_inferred_when_no_value_fits(
    games: list[tuple[int, int]],
) -> None:
    issues = _issues(games=games)
    assert [(issue.field, issue.code) for issue in issues] == [
        ("best_of", "cannot_infer")
    ]
    assert issues[0].message == "The games do not decide a best of 1, 3, or 5 match."


@pytest.mark.parametrize(
    ("winners", "expected"),
    [
        ([Team.A], BestOf.ONE),
        ([Team.B, Team.B], BestOf.THREE),
        ([Team.A, Team.B, Team.B], BestOf.THREE),
        ([Team.A, Team.A, Team.A], BestOf.FIVE),
        ([Team.A, Team.B, Team.A, Team.B, Team.A], BestOf.FIVE),
        ([Team.A, Team.B], None),
        ([], None),
        ([Team.A] * 6, None),
    ],
)
def test_infer_best_of(winners: list[Team], expected: BestOf | None) -> None:
    assert infer_best_of(winners) == expected


# Criterion 9: score types.
@pytest.mark.parametrize(
    ("score", "code"),
    [
        ((-1, 11), "negative_score"),
        ((11, -3), "negative_score"),
        ((11.0, 5), "invalid_score"),
        ((11, "5"), "invalid_score"),
        ((True, False), "invalid_score"),
        ((11, None), "invalid_score"),
        ((7, 7), "tie"),
    ],
)
def test_scores_must_be_non_negative_whole_numbers(
    score: tuple[object, object], code: str
) -> None:
    issues = _issues(games=[score])
    assert [(issue.field, issue.code) for issue in issues] == [("games.0", code)]
    assert "Game 1" in issues[0].message


@pytest.mark.parametrize("game", [(11,), (11, 5, 3), "11-5", None, 11])
def test_a_game_that_is_not_a_pair_of_scores_fails(game: object) -> None:
    assert _codes(games=[game]) == {("games.0", "invalid_score")}


def test_each_bad_game_is_reported_by_its_number() -> None:
    issues = _issues(best_of=3, games=[(11, 5), (11, 10), (12, 3)])
    assert [(issue.field, issue.code) for issue in issues] == [
        ("games.1", "not_won_by_two"),
        ("games.2", "played_past_target"),
    ]
    assert "Game 2" in issues[0].message
    assert "Game 3" in issues[1].message


# Criterion 10: game count.
def test_zero_games_fail() -> None:
    assert _codes(games=[]) == {("games", "no_games")}


def test_more_than_max_games_fail() -> None:
    games = [(11, 5), (5, 11)] * 3
    assert len(games) == MAX_GAMES + 1
    assert _codes(games=games, best_of=5) == {("games", "too_many_games")}


def test_max_games_pass() -> None:
    games = [(11, 5), (5, 11), (11, 5), (5, 11), (11, 5)]
    assert len(games) == MAX_GAMES
    assert _validate(games=games, best_of=5).games == tuple(games)


# Criterion 11: the date played.
def _latest_allowed_date() -> date:
    return (_NOW + timedelta(minutes=MAX_UTC_OFFSET_MINUTES)).date()


def test_the_latest_local_date_on_earth_passes() -> None:
    latest = _latest_allowed_date()
    assert latest == date(2026, 6, 16)
    assert _validate(played_on=latest).played_on == latest


def test_the_day_after_the_latest_local_date_fails() -> None:
    issues = _issues(played_on=_latest_allowed_date() + timedelta(days=1))
    assert [(issue.field, issue.code) for issue in issues] == [
        ("played_on", "future_date")
    ]
    assert "2026" not in issues[0].message


def test_the_future_check_uses_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    # 23:30 on June 15 at UTC-12 is 11:30 on June 16 in UTC, so the latest
    # local date anywhere (UTC+14) is June 17. Adding 14 hours to the local
    # time instead would give June 16.
    local = datetime(2026, 6, 15, 23, 30, tzinfo=dt_timezone(timedelta(hours=-12)))
    monkeypatch.setattr("apps.matches.services.timezone.now", lambda: local)
    assert _validate(played_on=date(2026, 6, 17)).played_on == date(2026, 6, 17)
    assert _outcome(played_on=date(2026, 6, 18)) == {("played_on", "future_date")}


@pytest.mark.parametrize(
    "played_on", [datetime(2026, 6, 1, tzinfo=UTC), "2026-06-01", None]
)
def test_played_on_must_be_a_date(played_on: object) -> None:
    issues = _issues(played_on=played_on)
    assert [(issue.field, issue.code) for issue in issues] == [("played_on", "invalid")]


# Criterion 13: every issue at once, and no submitted value in a message.
def test_every_issue_is_reported_together() -> None:
    issues = _issues(
        format=MatchFormat.DOUBLES,
        team_a=("player-a1", "player-a1"),
        team_b=("player-a1",),
        games=[(11, 10), (-2, 11)],
        played_on=date(2027, 1, 1),
        best_of=7,
        game_target=11,
        scoring_type="rally",
    )
    assert {(issue.field, issue.code) for issue in issues} == {
        ("team_a", "duplicate_player"),
        ("team_b", "team_size"),
        ("team_b", "player_on_both_teams"),
        ("games.0", "not_won_by_two"),
        ("games.1", "negative_score"),
        ("played_on", "future_date"),
        ("best_of", "invalid_choice"),
    }
    for issue in issues:
        assert "player-a1" not in issue.message


def test_the_error_text_is_a_fixed_summary() -> None:
    with pytest.raises(MatchValidationError) as caught:
        _validate(games=[(12, 9)])
    text = str(caught.value)
    assert text == "The match is not valid."


def test_issues_are_frozen() -> None:
    issue = MatchIssue(
        field="games", code="no_games", message="Enter at least one game."
    )
    with pytest.raises(AttributeError):
        issue.code = "other"  # type: ignore[misc]


# Criterion 14: the inputs are only the match itself.
def test_the_signature_takes_only_match_values() -> None:
    parameters = inspect.signature(validate_match).parameters
    assert set(parameters) == {
        "format",
        "team_a",
        "team_b",
        "games",
        "played_on",
        "best_of",
        "game_target",
        "scoring_type",
        "event_dates",
    }
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters.values()
    )


# The factories build matches these rules accept.
def _validate_stored(match: Match) -> ValidatedMatch:
    def team(side: str) -> tuple[Hashable, ...]:
        return tuple(
            participant.player.public_code
            for participant in match.participants.filter(team=side).order_by("pk")
        )

    return validate_match(
        format=match.format,
        team_a=team(Team.A),
        team_b=team(Team.B),
        games=[
            (game.team_a_score, game.team_b_score)
            for game in match.games.order_by("game_number")
        ],
        played_on=match.played_on,
        best_of=match.best_of,
        game_target=match.game_target,
        scoring_type=match.scoring_type,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("factory", [MatchFactory, DoublesMatchFactory])
@pytest.mark.parametrize("best_of", ["best_of_1", "best_of_3", "best_of_5"])
@pytest.mark.parametrize("scoring", ["side_out", "rally"])
def test_factory_matches_pass_validation(
    monkeypatch: pytest.MonkeyPatch,
    factory: type[MatchFactory],
    best_of: str,
    scoring: str,
) -> None:
    # Factory dates run up to today, so use the real clock.
    monkeypatch.setattr("apps.matches.services.timezone.now", _REAL_NOW)
    for _ in range(3):
        match = factory.create(**{best_of: True, scoring: True})
        result = _validate_stored(match)
        assert result.best_of == match.best_of
        assert result.scoring_type == match.scoring_type

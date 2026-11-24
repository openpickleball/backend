"""The CSV results format (#70): ``parse_results_csv`` and the template.

These tests need no database: the parser reads bytes and calls
``validate_match``, which takes plain values. Names and OPEN player IDs
are made up. The IDs carry valid check characters so they normalize.
"""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from apps.matches.csv_format import (
    COLUMNS,
    CsvFileError,
    CsvIssue,
    CsvMatch,
    CsvPlayer,
    CsvRowError,
    ParsedResults,
    parse_results_csv,
)
from apps.matches.models import MAX_GAMES, BestOf, GameTarget, MatchFormat, ScoringType

TEMPLATE = Path(__file__).resolve().parents[3] / "docs" / "csv-results-template.csv"

HEADER = [
    "matchType",
    "scoreType",
    "event",
    "date",
    "playerA1",
    "playerA1Id",
    "playerA2",
    "playerA2Id",
    "playerB1",
    "playerB1Id",
    "playerB2",
    "playerB2Id",
    "teamAGame1",
    "teamBGame1",
    "teamAGame2",
    "teamBGame2",
    "teamAGame3",
    "teamBGame3",
    "teamAGame4",
    "teamBGame4",
    "teamAGame5",
    "teamBGame5",
]

GAME_COLUMNS = tuple(HEADER[12:])

# Made-up OPEN player IDs with valid check characters.
P1 = "P-Z06Z-RMX0"
P2 = "P-8T34-SA1Q"
P3 = "P-TGK9-WK41"
P4 = "P-325K-X1J4"

# Well formed but with a failed check character: no player has it.
UNKNOWN_ID = "P-AAAA-AAAA"

PLAYED_ON = date(2024, 1, 5)

DOUBLES: dict[str, str] = {
    "matchType": "D",
    "scoreType": "SIDEOUT",
    "event": "Winter Ladder Round 1",
    "date": "2024-01-05",
    "playerA1": "Avery Lin",
    "playerA1Id": P1,
    "playerA2": "Jordan Park",
    "playerA2Id": P2,
    "playerB1": "Riley Moss",
    "playerB1Id": P3,
    "playerB2": "Casey Quinn",
    "playerB2Id": P4,
    "teamAGame1": "11",
    "teamBGame1": "7",
    "teamAGame2": "9",
    "teamBGame2": "11",
    "teamAGame3": "11",
    "teamBGame3": "6",
}

SINGLES: dict[str, str] = {
    "matchType": "S",
    "scoreType": "RALLY",
    "event": "Winter Singles",
    "date": "2024-01-06",
    "playerA1": "Avery Lin",
    "playerA1Id": P1,
    "playerB1": "Riley Moss",
    "playerB1Id": P3,
    "teamAGame1": "11",
    "teamBGame1": "4",
}


def _cells(values: dict[str, str]) -> list[str]:
    """A data row of 22 cells in column order, empty where not given."""
    assert set(values) <= set(HEADER)
    return [values.get(label, "") for label in HEADER]


def _with(base: dict[str, str], **changes: str) -> dict[str, str]:
    row = dict(base)
    row.update(changes)
    return row


def _encode(
    records: list[list[str]],
    *,
    newline: str = "\n",
    bom: bool = False,
    trailing_newline: bool = True,
) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator=newline)
    writer.writerows(records)
    text = buffer.getvalue()
    if not trailing_newline:
        text = text.removesuffix(newline)
    data = text.encode()
    return b"\xef\xbb\xbf" + data if bom else data


def _file(*rows: dict[str, str], header: list[str] | None = None) -> bytes:
    return _encode([header or HEADER, *(_cells(row) for row in rows)])


def _only_error(result: ParsedResults) -> CsvRowError:
    assert len(result.rows) == 1
    row = result.rows[0]
    assert isinstance(row, CsvRowError)
    return row


def _only_match(result: ParsedResults) -> CsvMatch:
    assert len(result.rows) == 1
    row = result.rows[0]
    assert isinstance(row, CsvMatch), row
    return row


def _pairs(error: CsvRowError) -> set[tuple[tuple[str, ...], str]]:
    return {(issue.columns, issue.code) for issue in error.issues}


# The columns.


def test_columns_are_the_layout_in_order() -> None:
    assert tuple(HEADER) == COLUMNS
    assert len(COLUMNS) == 12 + 2 * MAX_GAMES


# Rows that parse.


def test_doubles_row_parses() -> None:
    match = _only_match(parse_results_csv(_file(DOUBLES)))

    assert match == CsvMatch(
        row=2,
        sequence=1,
        format=MatchFormat.DOUBLES,
        scoring_type=ScoringType.SIDE_OUT,
        event_label="Winter Ladder Round 1",
        played_on=PLAYED_ON,
        team_a=(
            CsvPlayer(column="playerA1Id", name="Avery Lin", code=P1),
            CsvPlayer(column="playerA2Id", name="Jordan Park", code=P2),
        ),
        team_b=(
            CsvPlayer(column="playerB1Id", name="Riley Moss", code=P3),
            CsvPlayer(column="playerB2Id", name="Casey Quinn", code=P4),
        ),
        games=((11, 7), (9, 11), (11, 6)),
        best_of=BestOf.THREE,
        game_target=GameTarget.ELEVEN,
    )


def test_singles_row_parses() -> None:
    match = _only_match(parse_results_csv(_file(SINGLES)))

    assert match.format == MatchFormat.SINGLES
    assert match.scoring_type == ScoringType.RALLY
    assert match.played_on == date(2024, 1, 6)
    assert match.team_a == (CsvPlayer("playerA1Id", "Avery Lin", P1),)
    assert match.team_b == (CsvPlayer("playerB1Id", "Riley Moss", P3),)
    assert match.games == ((11, 4),)
    assert match.best_of == BestOf.ONE


def test_choice_cells_ignore_case_and_spaces() -> None:
    row = _with(SINGLES, matchType=" s ", scoreType=" rally ")
    match = _only_match(parse_results_csv(_file(row)))

    assert match.format == MatchFormat.SINGLES
    assert match.scoring_type == ScoringType.RALLY


def test_three_straight_wins_infer_best_of_five() -> None:
    row = _with(DOUBLES, teamAGame2="11", teamBGame2="5")
    match = _only_match(parse_results_csv(_file(row)))

    assert match.games == ((11, 7), (11, 5), (11, 6))
    assert match.best_of == BestOf.FIVE


def test_player_ids_are_normalized() -> None:
    row = _with(
        DOUBLES,
        playerA1Id=" p-z06z-rmx0 ",
        playerA2Id="8t34sa1q",
        playerB1Id="P TGK9 WK41",
    )
    match = _only_match(parse_results_csv(_file(row)))

    assert [player.code for player in match.team_a] == [P1, P2]
    assert match.team_b[0].code == P3


def test_names_and_event_are_stripped() -> None:
    row = _with(SINGLES, playerA1="  Avery Lin ", event="  Winter Singles  ")
    match = _only_match(parse_results_csv(_file(row)))

    assert match.team_a[0].name == "Avery Lin"
    assert match.event_label == "Winter Singles"


def test_empty_name_with_an_id_is_allowed() -> None:
    row = _with(SINGLES, playerA1="")
    match = _only_match(parse_results_csv(_file(row)))

    assert match.team_a == (CsvPlayer("playerA1Id", "", P1),)


def test_empty_event_is_allowed() -> None:
    match = _only_match(parse_results_csv(_file(_with(SINGLES, event=""))))

    assert match.event_label == ""


# The header.


def _header_with_id_middle(middle: str) -> list[str]:
    header = list(HEADER)
    for index in (5, 7, 9, 11):
        label = HEADER[index]
        header[index] = label[: -len("Id")] + middle + "Id"
    return header


@pytest.mark.parametrize("middle", ["External", "Rating", "Ext_2", ""])
def test_id_headers_accept_any_middle(middle: str) -> None:
    header = _header_with_id_middle(middle)
    result = parse_results_csv(_file(DOUBLES, header=header))

    assert _only_match(result).team_a[0].code == P1


def test_headers_ignore_case_and_spaces() -> None:
    header = [f" {label.upper()} " for label in HEADER]
    header[5] = "PLAYERA1EXTERNALID"
    result = parse_results_csv(_file(DOUBLES, header=header))

    assert _only_match(result).format == MatchFormat.DOUBLES


def test_trailing_empty_header_cells_are_ignored() -> None:
    result = parse_results_csv(_file(DOUBLES, header=[*HEADER, "", " "]))

    assert _only_match(result).format == MatchFormat.DOUBLES


def _swapped(first: int, second: int) -> list[str]:
    header = list(HEADER)
    header[first], header[second] = header[second], header[first]
    return header


def _replaced(index: int, text: str) -> list[str]:
    header = list(HEADER)
    header[index] = text
    return header


@pytest.mark.parametrize(
    ("header", "sent_text"),
    [
        pytest.param(_swapped(4, 5), None, id="id-column-moved"),
        pytest.param(_replaced(5, "playerA1Nickname"), "Nickname", id="name-in-id"),
        pytest.param(_replaced(5, "playerB1Id"), None, id="wrong-team-id"),
        pytest.param(_replaced(5, "playerA1 Id"), None, id="space-in-id"),
        pytest.param(_replaced(1, "scoringMode"), "scoringMode", id="renamed"),
        pytest.param(HEADER[:-1], None, id="missing-column"),
        pytest.param([*HEADER, "notesColumn"], "notesColumn", id="extra-column"),
    ],
)
def test_bad_header_is_refused(header: list[str], sent_text: str | None) -> None:
    with pytest.raises(CsvFileError) as caught:
        parse_results_csv(_file(DOUBLES, header=header))

    assert caught.value.code == "bad_header"
    if sent_text is not None:
        assert sent_text not in caught.value.message


def test_bad_header_names_the_position_and_label() -> None:
    with pytest.raises(CsvFileError) as caught:
        parse_results_csv(_file(DOUBLES, header=_replaced(5, "playerA1Nickname")))

    assert "6" in caught.value.message
    assert "playerA1Id" in caught.value.message


def test_data_row_first_is_refused() -> None:
    data = _encode([_cells(DOUBLES), _cells(DOUBLES)])

    with pytest.raises(CsvFileError) as caught:
        parse_results_csv(data)

    assert caught.value.code == "bad_header"
    assert "Avery Lin" not in caught.value.message
    assert P1 not in caught.value.message


# Encoding and layout.


def test_bom_gives_the_same_result() -> None:
    records = [HEADER, _cells(DOUBLES), _cells(SINGLES)]

    assert parse_results_csv(_encode(records, bom=True)) == parse_results_csv(
        _encode(records)
    )


def test_crlf_gives_the_same_result() -> None:
    records = [HEADER, _cells(DOUBLES), _cells(SINGLES)]

    assert parse_results_csv(_encode(records, newline="\r\n")) == parse_results_csv(
        _encode(records)
    )


def test_missing_trailing_newline_gives_the_same_result() -> None:
    records = [HEADER, _cells(DOUBLES), _cells(SINGLES)]

    assert parse_results_csv(
        _encode(records, trailing_newline=False)
    ) == parse_results_csv(_encode(records))


def test_bom_and_crlf_together_parse() -> None:
    records = [_header_with_id_middle("External"), _cells(DOUBLES)]
    result = parse_results_csv(_encode(records, newline="\r\n", bom=True))

    assert _only_match(result).best_of == BestOf.THREE


def test_rows_cut_short_after_the_last_game_parse() -> None:
    cells = _cells(SINGLES)[:14]
    result = parse_results_csv(_encode([HEADER, cells]))

    assert _only_match(result).games == ((11, 4),)


def test_extra_empty_cells_are_trimmed() -> None:
    cells = [*_cells(SINGLES), "", " "]
    result = parse_results_csv(_encode([HEADER, cells]))

    assert _only_match(result).games == ((11, 4),)


def test_quoted_name_with_comma_and_apostrophe() -> None:
    row = _with(DOUBLES, playerA1="O'Neal, Morgan")
    data = _file(row)
    assert b'"O\'Neal, Morgan"' in data

    match = _only_match(parse_results_csv(data))

    assert match.team_a[0].name == "O'Neal, Morgan"


def test_blank_lines_are_skipped_without_a_sequence() -> None:
    data = _encode([HEADER, _cells(DOUBLES), [], ["", " "], _cells(SINGLES)])
    result = parse_results_csv(data)

    assert [(m.row, m.sequence) for m in result.matches] == [(2, 1), (5, 2)]


def test_not_utf8_is_refused() -> None:
    data = _file(_with(DOUBLES, playerA1="Zoe")).replace(b"Zoe", b"Zo\xeb")

    with pytest.raises(CsvFileError) as caught:
        parse_results_csv(data)

    assert caught.value.code == "not_utf8"


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(b'D,"SIDE"OUT,\n', id="bad-quote"),
        pytest.param(b'D,"SIDEOUT\n', id="unclosed-quote"),
        pytest.param(b"D,SIDE\x00OUT\n", id="nul"),
    ],
)
def test_malformed_csv_is_refused(tail: bytes) -> None:
    with pytest.raises(CsvFileError) as caught:
        parse_results_csv(_file(DOUBLES) + tail)

    assert caught.value.code == "malformed"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_encode([HEADER]), id="header-only"),
        pytest.param(_encode([HEADER, [], [""] * 22]), id="blank-rows-only"),
    ],
)
def test_no_rows_is_refused(data: bytes) -> None:
    with pytest.raises(CsvFileError) as caught:
        parse_results_csv(data)

    assert caught.value.code == "no_rows"


# Row errors.

_ALL_GAMES_EMPTY = {label: "" for label in GAME_COLUMNS}


@pytest.mark.parametrize(
    ("changes", "columns", "code", "sent"),
    [
        pytest.param(
            {"scoreType": "BOGUS"},
            ("scoreType",),
            "invalid_choice",
            "BOGUS",
            id="unknown-score-type",
        ),
        pytest.param(
            {"scoreType": ""},
            ("scoreType",),
            "invalid_choice",
            None,
            id="no-score-type",
        ),
        pytest.param(
            {"matchType": "Q"}, ("matchType",), "invalid_choice", "Q", id="match-type"
        ),
        pytest.param(
            {"teamAGame1": "11", "teamBGame1": "5", "teamAGame3": "", "teamBGame3": ""},
            GAME_COLUMNS,
            "cannot_infer",
            None,
            id="not-decided",
        ),
        pytest.param(
            {
                "teamAGame1": "11",
                "teamBGame1": "5",
                "teamAGame2": "11",
                "teamBGame2": "5",
                "teamAGame3": "5",
                "teamBGame3": "11",
            },
            GAME_COLUMNS,
            "cannot_infer",
            None,
            id="game-after-decided",
        ),
        pytest.param(
            {"playerB1Id": UNKNOWN_ID},
            ("playerB1Id",),
            "unknown_player",
            UNKNOWN_ID,
            id="unknown-id",
        ),
        pytest.param(
            {"playerB1Id": "Riley Moss"},
            ("playerB1Id",),
            "unknown_player",
            "Riley Moss",
            id="name-as-id",
        ),
        pytest.param(
            {"playerA2Id": P1},
            ("playerA1Id", "playerA2Id"),
            "duplicate_player",
            P1,
            id="same-player-twice",
        ),
        pytest.param(
            {"playerB1Id": P1},
            ("playerB1Id", "playerB2Id"),
            "player_on_both_teams",
            P1,
            id="player-on-both-teams",
        ),
        pytest.param(
            {"playerA2Id": ""},
            ("playerA2Id",),
            "missing_player_id",
            None,
            id="name-without-id",
        ),
        pytest.param(
            {"teamAGame2": "abc"},
            ("teamAGame2", "teamBGame2"),
            "invalid_score",
            "abc",
            id="letters",
        ),
        pytest.param(
            {"teamAGame2": "٣"},
            ("teamAGame2", "teamBGame2"),
            "invalid_score",
            "٣",
            id="other-script-digit",
        ),
        pytest.param(
            {"teamAGame2": "+9"},
            ("teamAGame2", "teamBGame2"),
            "invalid_score",
            "+9",
            id="plus-sign",
        ),
        pytest.param(
            {"teamAGame2": "9.0"},
            ("teamAGame2", "teamBGame2"),
            "invalid_score",
            "9.0",
            id="decimal",
        ),
        pytest.param(
            {"teamAGame2": "1_0"},
            ("teamAGame2", "teamBGame2"),
            "invalid_score",
            "1_0",
            id="underscore",
        ),
        pytest.param(
            {"teamAGame2": "1 0"},
            ("teamAGame2", "teamBGame2"),
            "invalid_score",
            "1 0",
            id="inner-space",
        ),
        pytest.param(
            {"teamAGame2": "1234567890"},
            ("teamAGame2", "teamBGame2"),
            "invalid_score",
            "1234567890",
            id="too-many-digits",
        ),
        pytest.param(
            {"teamBGame2": "-7"},
            ("teamAGame2", "teamBGame2"),
            "negative_score",
            "-7",
            id="negative",
        ),
        pytest.param(
            {"teamAGame1": "11", "teamBGame1": "11"},
            ("teamAGame1", "teamBGame1"),
            "tie",
            None,
            id="tie",
        ),
        pytest.param(
            {"teamAGame1": "15", "teamBGame1": "10"},
            ("teamAGame1", "teamBGame1"),
            "played_past_target",
            None,
            id="past-target",
        ),
        pytest.param(
            {"teamBGame3": ""},
            ("teamAGame3", "teamBGame3"),
            "incomplete_game",
            None,
            id="incomplete-game",
        ),
        pytest.param(
            {"teamAGame2": "", "teamBGame2": ""},
            ("teamAGame2", "teamBGame2"),
            "missing_game",
            None,
            id="missing-middle-game",
        ),
        pytest.param(_ALL_GAMES_EMPTY, GAME_COLUMNS, "no_games", None, id="no-games"),
        pytest.param(
            {"event": "Ladder " * 30},
            ("event",),
            "too_long",
            "Ladder " * 30,
            id="event-too-long",
        ),
        pytest.param(
            {"date": "2024/01/05"}, ("date",), "invalid_date", "2024/01/05", id="slash"
        ),
        pytest.param(
            {"date": "20240105"}, ("date",), "invalid_date", "20240105", id="compact"
        ),
        pytest.param(
            {"date": "2024-02-30"},
            ("date",),
            "invalid_date",
            "2024-02-30",
            id="no-such-day",
        ),
        pytest.param(
            {"date": "2024-1-5"}, ("date",), "invalid_date", "2024-1-5", id="short"
        ),
        pytest.param({"date": ""}, ("date",), "invalid_date", None, id="no-date"),
        pytest.param(
            {"date": (datetime.now(UTC).date() + timedelta(days=3)).isoformat()},
            ("date",),
            "future_date",
            None,
            id="future-date",
        ),
        pytest.param(
            {
                "playerA2": "",
                "playerA2Id": "",
                "playerB2": "",
                "playerB2Id": "",
            },
            ("playerA1Id",),
            "team_size",
            None,
            id="doubles-with-one-player-a-side",
        ),
        pytest.param(
            {"matchType": "S"},
            ("playerA1Id", "playerA2Id"),
            "team_size",
            None,
            id="singles-with-two-players-a-side",
        ),
    ],
)
def test_row_errors(
    changes: dict[str, str],
    columns: tuple[str, ...],
    code: str,
    sent: str | None,
) -> None:
    error = _only_error(parse_results_csv(_file(_with(DOUBLES, **changes))))

    assert (columns, code) in _pairs(error)
    assert error.row == 2
    assert error.sequence == 1
    for issue in error.issues:
        assert isinstance(issue, CsvIssue)
        assert issue.message
        if sent is not None:
            assert sent.strip() not in issue.message


def test_extra_cells_with_values_fail_the_row() -> None:
    cells = [*_cells(DOUBLES), "", "a note"]
    error = _only_error(parse_results_csv(_encode([HEADER, cells])))

    assert _pairs(error) == {((), "extra_columns")}
    assert all("a note" not in issue.message for issue in error.issues)


def test_unknown_id_is_the_only_issue() -> None:
    row = _with(DOUBLES, playerA1Id=UNKNOWN_ID, playerA2Id=UNKNOWN_ID)
    error = _only_error(parse_results_csv(_file(row)))

    assert _pairs(error) == {
        (("playerA1Id",), "unknown_player"),
        (("playerA2Id",), "unknown_player"),
    }


def test_missing_id_is_the_only_issue() -> None:
    error = _only_error(parse_results_csv(_file(_with(DOUBLES, playerB2Id=""))))

    assert _pairs(error) == {(("playerB2Id",), "missing_player_id")}


def test_bad_match_type_is_the_only_issue() -> None:
    error = _only_error(parse_results_csv(_file(_with(DOUBLES, matchType="Q"))))

    assert _pairs(error) == {(("matchType",), "invalid_choice")}


def test_bad_date_is_the_only_issue() -> None:
    error = _only_error(parse_results_csv(_file(_with(DOUBLES, date="05/01/2024"))))

    assert _pairs(error) == {(("date",), "invalid_date")}


def test_bad_score_is_the_only_issue() -> None:
    error = _only_error(parse_results_csv(_file(_with(DOUBLES, teamBGame3="x"))))

    assert _pairs(error) == {(("teamAGame3", "teamBGame3"), "invalid_score")}


def test_cell_issues_come_before_match_issues() -> None:
    row = _with(DOUBLES, scoreType="BOGUS", playerA2="", playerA2Id="")
    error = _only_error(parse_results_csv(_file(row)))

    assert [issue.code for issue in error.issues] == ["invalid_choice", "team_size"]
    assert error.issues[0].columns == ("scoreType",)
    assert error.issues[1].columns == ("playerA1Id",)


def test_bad_row_does_not_stop_later_rows() -> None:
    bad = _with(DOUBLES, scoreType="BOGUS")
    result = parse_results_csv(_file(DOUBLES, bad, SINGLES))

    assert [type(row) for row in result.rows] == [CsvMatch, CsvRowError, CsvMatch]
    assert [(m.row, m.sequence) for m in result.matches] == [(2, 1), (4, 3)]
    assert [(e.row, e.sequence) for e in result.errors] == [(3, 2)]


# The game target.


def test_target_fifteen_accepts_a_game_to_fifteen() -> None:
    row = _with(SINGLES, teamAGame1="15", teamBGame1="10")

    default = _only_error(parse_results_csv(_file(row)))
    assert (("teamAGame1", "teamBGame1"), "played_past_target") in _pairs(default)

    match = _only_match(parse_results_csv(_file(row), game_target=GameTarget.FIFTEEN))
    assert match.game_target == GameTarget.FIFTEEN
    assert match.games == ((15, 10),)


def test_target_twenty_one_accepts_a_game_to_twenty_one() -> None:
    row = _with(SINGLES, teamAGame1="21", teamBGame1="15")
    match = _only_match(
        parse_results_csv(_file(row), game_target=GameTarget.TWENTY_ONE)
    )

    assert match.game_target == GameTarget.TWENTY_ONE


def test_target_fifteen_refuses_a_game_to_eleven() -> None:
    error = _only_error(
        parse_results_csv(_file(SINGLES), game_target=GameTarget.FIFTEEN)
    )

    assert (("teamAGame1", "teamBGame1"), "target_not_reached") in _pairs(error)


@pytest.mark.parametrize("target", [0, 12, 25])
def test_target_outside_the_choices_is_a_caller_error(target: int) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse_results_csv(_file(SINGLES), game_target=target)  # type: ignore[arg-type]


# The template.


def test_template_parses_with_no_errors() -> None:
    result = parse_results_csv(TEMPLATE.read_bytes())

    assert result.errors == ()
    matches = result.matches
    assert {match.format for match in matches} == {
        MatchFormat.SINGLES,
        MatchFormat.DOUBLES,
    }
    assert ScoringType.RALLY in {match.scoring_type for match in matches}
    assert BestOf.THREE in {match.best_of for match in matches}
    assert [match.sequence for match in matches] == list(range(1, len(matches) + 1))

"""Review fixes for the CSV results parser (#70).

Negative zero scores, non-ASCII cells and headers that fold to ASCII
when their case changes, and issues on games before the first bad game.
Made-up names and OPEN player IDs only; no database.
"""

import csv
import io

import pytest

from apps.matches.csv_format import (
    COLUMNS,
    CsvFileError,
    CsvRowError,
    parse_results_csv,
)

P1 = "P-Z06Z-RMX0"
P2 = "P-8T34-SA1Q"
P3 = "P-TGK9-WK41"
P4 = "P-325K-X1J4"

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
}


def _file(row: dict[str, str], header: list[str] | None = None) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header or list(COLUMNS))
    writer.writerow([row.get(label, "") for label in COLUMNS])
    return buffer.getvalue().encode()


def _error(**changes: str) -> CsvRowError:
    result = parse_results_csv(_file({**DOUBLES, **changes}))
    assert len(result.rows) == 1
    row = result.rows[0]
    assert isinstance(row, CsvRowError), row
    return row


def _pairs(error: CsvRowError) -> set[tuple[tuple[str, ...], str]]:
    return {(issue.columns, issue.code) for issue in error.issues}


@pytest.mark.parametrize("value", ["-0", "-00", " -0 "])
def test_negative_zero_is_an_invalid_score(value: str) -> None:
    error = _error(teamBGame1=value)

    assert _pairs(error) == {(("teamAGame1", "teamBGame1"), "invalid_score")}


def test_leading_zero_is_still_a_score() -> None:
    result = parse_results_csv(_file({**DOUBLES, "teamBGame1": "07"}))

    assert result.errors == ()
    assert result.matches[0].games == ((11, 7),)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("matchType", "\u017f"),
        ("scoreType", "\u017fideout"),
        ("scoreType", "s\u0131deout"),
    ],
)
def test_non_ascii_choice_cells_are_refused(column: str, value: str) -> None:
    error = _error(**{column: value})

    assert ((column,), "invalid_choice") in _pairs(error)
    assert all(value not in issue.message for issue in error.issues)


@pytest.mark.parametrize(
    ("index", "text"),
    [
        (1, "\u017fcoreType"),
        (5, "playerA1\u212aId"),
        (5, "playerA1\u0131d"),
        (7, "playerA2Ext\u0131Id"),
        (0, "matchTyp\u0435"),
    ],
)
def test_non_ascii_header_cells_are_refused(index: int, text: str) -> None:
    header = list(COLUMNS)
    header[index] = text

    with pytest.raises(CsvFileError) as caught:
        parse_results_csv(_file(DOUBLES, header=header))

    assert caught.value.code == "bad_header"
    assert text not in caught.value.message


def test_issue_on_a_game_before_a_bad_game_is_kept() -> None:
    error = _error(teamAGame1="11", teamBGame1="11", teamAGame2="abc", teamBGame2="5")

    assert _pairs(error) == {
        (("teamAGame2", "teamBGame2"), "invalid_score"),
        (("teamAGame1", "teamBGame1"), "tie"),
    }


def test_issue_on_a_game_after_a_bad_game_is_not_reported() -> None:
    error = _error(
        teamAGame1="abc",
        teamBGame1="5",
        teamAGame2="11",
        teamBGame2="11",
    )

    assert _pairs(error) == {(("teamAGame1", "teamBGame1"), "invalid_score")}


def test_best_of_is_not_reported_when_a_game_is_bad() -> None:
    error = _error(
        teamAGame1="11",
        teamBGame1="5",
        teamAGame2="9",
        teamBGame2="11",
        teamAGame3="11",
        teamBGame3="",
    )

    assert _pairs(error) == {(("teamAGame3", "teamBGame3"), "incomplete_game")}

"""The CSV results format (#70): read an organizer's results file.

``parse_results_csv`` reads a CSV file in the 22 column results layout
that ``docs/csv-format.md`` describes, the layout a standard external
rating system's export uses. Each data row becomes a ``CsvMatch`` (the
row passed) or a ``CsvRowError`` (every issue found in the row). The
upload (#71) looks up each ``CsvPlayer.code``, compares names with
display names, and records each ``CsvMatch`` with ``record_match()`` in
its own transaction.

The parser never reads the database. It checks the cells it reads (the
choices, the date, the OPEN player IDs, which it normalizes with
``apps.public_ids.codes.normalize()``, and the score cells), then runs
every row through ``validate_match()``, so a row gets the same score
rules as an API submission. A row's issues name its cells by their
canonical column labels.

Every message is fixed text, a column label, a column position, a game
number, or an allowed value. No message ever holds a submitted value
(CLAUDE.md, API), so a report can show them as they are.
"""

import csv
import io
import re
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, cast

from apps.matches.models import (
    MAX_GAMES,
    BestOf,
    GameTarget,
    Match,
    MatchFormat,
    ScoringType,
)
from apps.matches.services import MatchIssue, MatchValidationError, validate_match
from apps.public_ids.codes import CodeType, normalize

MATCH_TYPES: Final[dict[str, MatchFormat]] = {
    "S": MatchFormat.SINGLES,
    "D": MatchFormat.DOUBLES,
}
SCORE_TYPES: Final[dict[str, ScoringType]] = {
    "SIDEOUT": ScoringType.SIDE_OUT,
    "RALLY": ScoringType.RALLY,
}

TEAMS: Final = ("A", "B")
SLOTS: Final = (1, 2)


def _name_label(team: str, slot: int) -> str:
    return f"player{team}{slot}"


def _id_label(team: str, slot: int) -> str:
    return f"player{team}{slot}Id"


def _game_labels(number: int) -> tuple[str, str]:
    return f"teamAGame{number}", f"teamBGame{number}"


GAME_NUMBERS: Final = tuple(range(1, MAX_GAMES + 1))

COLUMNS: Final[tuple[str, ...]] = (
    "matchType",
    "scoreType",
    "event",
    "date",
    *(
        label
        for team in TEAMS
        for slot in SLOTS
        for label in (_name_label(team, slot), _id_label(team, slot))
    ),
    *(label for number in GAME_NUMBERS for label in _game_labels(number)),
)
assert len(COLUMNS) == 12 + 2 * MAX_GAMES

GAME_COLUMNS: Final[tuple[str, ...]] = COLUMNS[12:]

EVENT_LABEL_MAX: Final = cast(int, Match._meta.get_field("event_label").max_length)

# An ID column's header: the team and slot, any letters, digits, or
# underscores, then "Id".
_ID_HEADERS: Final[dict[int, re.Pattern[str]]] = {
    COLUMNS.index(_id_label(team, slot)): re.compile(
        rf"player{team}{slot}[A-Za-z0-9_]*Id", re.IGNORECASE
    )
    for team in TEAMS
    for slot in SLOTS
}

# ASCII digits only: int() takes other scripts' digits, signs, and
# underscores.
_SCORE: Final = re.compile(r"-?[0-9]{1,9}")
_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# validate_match's fields and the columns their issues point to.
_FIELD_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "format": ("matchType",),
    "scoring_type": ("scoreType",),
    "played_on": ("date",),
    "games": GAME_COLUMNS,
    "best_of": GAME_COLUMNS,
    "game_target": (),
}


class CsvFileError(Exception):
    """The file as a whole cannot be read. ``message`` is fixed text.

    ``code`` is ``not_utf8``, ``malformed``, ``bad_header``, or
    ``no_rows``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CsvIssue:
    """One problem with a row.

    ``columns`` holds the canonical labels of the cells concerned, and is
    empty for an issue with the whole row.
    """

    columns: tuple[str, ...]
    code: str
    message: str


@dataclass(frozen=True)
class CsvPlayer:
    """One player in a row.

    ``column`` is the canonical ID column label, ``name`` the stripped name
    cell (informational only, may be empty), and ``code`` the normalized
    OPEN player ID. No player has been looked up yet.
    """

    column: str
    name: str
    code: str


@dataclass(frozen=True)
class CsvMatch:
    """A row that passed every check, ready for #71 to look up and record.

    ``row`` is the record's position in the file (the header is row 1).
    ``sequence`` is the row's position among the data rows, the order of
    play stored on the Match.
    """

    row: int
    sequence: int
    format: MatchFormat
    scoring_type: ScoringType
    event_label: str
    played_on: date
    team_a: tuple[CsvPlayer, ...]
    team_b: tuple[CsvPlayer, ...]
    games: tuple[tuple[int, int], ...]
    best_of: BestOf
    game_target: GameTarget


@dataclass(frozen=True)
class CsvRowError:
    """A row that failed. ``issues`` is never empty."""

    row: int
    sequence: int
    issues: tuple[CsvIssue, ...]


@dataclass(frozen=True)
class ParsedResults:
    """Every data row of a file, in file order."""

    rows: tuple[CsvMatch | CsvRowError, ...]

    @property
    def matches(self) -> tuple[CsvMatch, ...]:
        return tuple(row for row in self.rows if isinstance(row, CsvMatch))

    @property
    def errors(self) -> tuple[CsvRowError, ...]:
        return tuple(row for row in self.rows if isinstance(row, CsvRowError))


def _is_blank(record: Sequence[str]) -> bool:
    return all(not cell.strip() for cell in record)


def _check_header(record: list[str]) -> None:
    cells = [cell.strip() for cell in record]
    while cells and not cells[-1]:
        cells.pop()
    if len(cells) != len(COLUMNS):
        raise CsvFileError(
            "bad_header",
            f"The first row must be the header, with {len(COLUMNS)} columns.",
        )
    for index, (cell, label) in enumerate(zip(cells, COLUMNS, strict=True)):
        pattern = _ID_HEADERS.get(index)
        if pattern is not None:
            ok = pattern.fullmatch(cell) is not None
        else:
            ok = cell.casefold() == label.casefold()
        if not ok:
            raise CsvFileError(
                "bad_header", f"Column {index + 1} must be the {label} column."
            )


def _records(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvFileError("not_utf8", "Save the file as UTF-8.") from None
    malformed = CsvFileError("malformed", "The file is not a valid CSV file.")
    if "\x00" in text:
        raise malformed
    try:
        return list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error:
        raise malformed from None


@dataclass
class _Row:
    """A data row while it is checked."""

    issues: list[CsvIssue]
    games_failed: bool = False


def _parse_choice[T](
    value: str, choices: dict[str, T], column: str, message: str, row: _Row
) -> T | None:
    choice = choices.get(value.strip().upper())
    if choice is None:
        row.issues.append(CsvIssue((column,), "invalid_choice", message))
    return choice


def _parse_date(value: str, row: _Row) -> date | None:
    text = value.strip()
    if _DATE.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    row.issues.append(
        CsvIssue(("date",), "invalid_date", "Enter the date as YYYY-MM-DD.")
    )
    return None


def _parse_team(
    team: str, cells: dict[str, str], row: _Row
) -> tuple[list[CsvPlayer], list[str], list[str]]:
    """A team's players, its references for validate_match, and its columns.

    A slot whose ID failed gets a reference unique to its column that no
    OPEN player ID can equal, so the team size and duplicate checks still
    run without a false match.
    """
    players: list[CsvPlayer] = []
    references: list[str] = []
    columns: list[str] = []
    for slot in SLOTS:
        column = _id_label(team, slot)
        name = cells[_name_label(team, slot)].strip()
        raw = cells[column].strip()
        if not raw and not name:
            continue
        columns.append(column)
        if not raw:
            row.issues.append(
                CsvIssue(
                    (column,),
                    "missing_player_id",
                    "Enter an OPEN player ID for this player.",
                )
            )
            references.append(f"invalid:{column}")
            continue
        code = normalize(raw, CodeType.PLAYER)
        if code is None:
            row.issues.append(
                CsvIssue(
                    (column,), "unknown_player", "No player has this OPEN player ID."
                )
            )
            references.append(f"invalid:{column}")
            continue
        players.append(CsvPlayer(column=column, name=name, code=code))
        references.append(code)
    return players, references, columns


def _parse_games(cells: dict[str, str], row: _Row) -> list[tuple[int, int]]:
    """The games that parsed, in order, up to the first game with an issue."""
    games: list[tuple[int, int]] = []
    filled = [
        any(cells[label].strip() for label in _game_labels(number))
        for number in GAME_NUMBERS
    ]
    for index, number in enumerate(GAME_NUMBERS):
        labels = _game_labels(number)
        values = [cells[label].strip() for label in labels]
        issue: CsvIssue | None = None
        if not any(values):
            if any(filled[index + 1 :]):
                issue = CsvIssue(
                    labels,
                    "missing_game",
                    f"Game {number} is empty but a later game has scores.",
                )
            else:
                continue
        elif not all(values):
            issue = CsvIssue(
                labels, "incomplete_game", f"Game {number}: enter both scores."
            )
        elif not all(_SCORE.fullmatch(value) for value in values):
            issue = CsvIssue(
                labels,
                "invalid_score",
                f"Game {number}: enter each score as a whole number.",
            )
        if issue is not None:
            row.issues.append(issue)
            row.games_failed = True
            continue
        if not row.games_failed:
            games.append((int(values[0]), int(values[1])))
    return games


def _match_issue_columns(
    issue: MatchIssue, team_columns: dict[str, list[str]]
) -> tuple[str, ...]:
    if issue.field in team_columns:
        columns = team_columns[issue.field]
        if columns:
            return tuple(columns)
        team = "A" if issue.field == "team_a" else "B"
        return tuple(_id_label(team, slot) for slot in SLOTS)
    if issue.field.startswith("games."):
        return _game_labels(int(issue.field.removeprefix("games.")) + 1)
    return _FIELD_COLUMNS.get(issue.field, ())


def _parse_row(
    record: list[str], row_number: int, sequence: int, game_target: GameTarget
) -> CsvMatch | CsvRowError:
    row = _Row(issues=[])
    extra = record[len(COLUMNS) :]
    if any(cell.strip() for cell in extra):
        row.issues.append(
            CsvIssue((), "extra_columns", "The row has more cells than the header.")
        )
    padded = record[: len(COLUMNS)] + [""] * (len(COLUMNS) - len(record))
    cells = dict(zip(COLUMNS, padded, strict=True))

    match_format = _parse_choice(
        cells["matchType"],
        MATCH_TYPES,
        "matchType",
        "Enter S for singles or D for doubles.",
        row,
    )
    scoring = _parse_choice(
        cells["scoreType"], SCORE_TYPES, "scoreType", "Enter SIDEOUT or RALLY.", row
    )
    event_label = cells["event"].strip()
    if len(event_label) > EVENT_LABEL_MAX:
        row.issues.append(
            CsvIssue(
                ("event",),
                "too_long",
                f"Enter an event label of at most {EVENT_LABEL_MAX} characters.",
            )
        )
    played_on = _parse_date(cells["date"], row)
    team_a, references_a, columns_a = _parse_team("A", cells, row)
    team_b, references_b, columns_b = _parse_team("B", cells, row)
    games = _parse_games(cells, row)

    # Fields whose cells already have an issue: validate_match's issue on
    # the substitute value would only repeat it.
    skipped: set[str] = set()
    if match_format is None:
        skipped.add("format")
    if scoring is None:
        skipped.add("scoring_type")
    if played_on is None:
        skipped.add("played_on")

    match_issues: list[CsvIssue] = []
    validated = None
    try:
        validated = validate_match(
            format=match_format or "",
            team_a=cast(list[Hashable], references_a),
            team_b=cast(list[Hashable], references_b),
            games=games,
            played_on=cast(date, played_on),
            game_target=game_target,
            scoring_type=scoring,
        )
    except MatchValidationError as error:
        team_columns = {"team_a": columns_a, "team_b": columns_b}
        for issue in error.issues:
            if issue.field in skipped:
                continue
            if row.games_failed and (
                issue.field in ("games", "best_of") or issue.field.startswith("games.")
            ):
                continue
            match_issues.append(
                CsvIssue(
                    _match_issue_columns(issue, team_columns),
                    issue.code,
                    issue.message,
                )
            )

    order = {label: index for index, label in enumerate(COLUMNS)}

    def position(issue: CsvIssue) -> int:
        return min((order[column] for column in issue.columns), default=-1)

    issues = sorted(row.issues, key=position) + match_issues
    if issues or validated is None:
        # validate_match raises whenever it returns nothing.
        return CsvRowError(row=row_number, sequence=sequence, issues=tuple(issues))
    return CsvMatch(
        row=row_number,
        sequence=sequence,
        format=validated.format,
        scoring_type=validated.scoring_type,
        event_label=event_label,
        played_on=validated.played_on,
        team_a=tuple(team_a),
        team_b=tuple(team_b),
        games=validated.games,
        best_of=validated.best_of,
        game_target=validated.game_target,
    )


def parse_results_csv(
    data: bytes, *, game_target: GameTarget = GameTarget.ELEVEN
) -> ParsedResults:
    """Read a results file and check every data row.

    ``game_target`` is the upload's game target (11, 15, or 21). Raises
    ``ValueError`` for any other target (a caller's bug) and
    ``CsvFileError`` when the file as a whole cannot be read. A row's
    problems never raise: they come back as a ``CsvRowError``.
    """
    if game_target not in GameTarget.values:
        raise ValueError("game_target must be a GameTarget value.")
    target = GameTarget(game_target)

    rows: list[CsvMatch | CsvRowError] = []
    header_seen = False
    sequence = 0
    for row_number, record in enumerate(_records(data), start=1):
        if _is_blank(record):
            continue
        if not header_seen:
            _check_header(record)
            header_seen = True
            continue
        sequence += 1
        rows.append(_parse_row(record, row_number, sequence, target))
    if not rows:
        raise CsvFileError("no_rows", "The file has no match rows.")
    return ParsedResults(rows=tuple(rows))

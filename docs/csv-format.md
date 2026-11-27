# CSV results format

This page describes the CSV file an organizer uploads to send match results to OPEN. It is for organizers and for the people who maintain the upload (#71).

OPEN reads the 22 column layout that a standard external rating system's export uses. An organizer can upload the same file to both systems, with no edits, as long as the player ID columns hold OPEN player IDs.

A template with made-up players is in [`csv-results-template.csv`](csv-results-template.csv).

## One submission schema, one set of rules

The CSV is an input layout for OPEN's one match submission schema (#80). It is not a second format. Each row becomes one match submission, and each row goes through the same validation as an API submission (`validate_match`). The column-to-field table below gives the field each column fills. The field names are `validate_match`'s and `record_match`'s argument names. `format`, `scoring_type`, `played_on`, `team_a`, `team_b`, and `games` are also the field names of the first-party `POST /api/v1/matches/` body (`MatchCreateIn`). `event_label` is a `record_match` argument only, like the upload's `event`, `location`, and the row's `sequence`.

`apps/matches/csv_format.py` reads the file (`parse_results_csv`). The upload (#71) then looks up the players and records each match.

## The columns

Every file has a header row and then one row per match. The columns come in this order.

| # | Column | Field | Required | What to enter |
| --- | --- | --- | --- | --- |
| 1 | `matchType` | `format` | Yes | `S` for singles or `D` for doubles. |
| 2 | `scoreType` | `scoring_type` | Yes | `SIDEOUT` or `RALLY`. |
| 3 | `event` | `event_label` | No | A short label, such as a round or a bracket name. At most 200 characters. |
| 4 | `date` | `played_on` | Yes | The date played, as `YYYY-MM-DD`. |
| 5 | `playerA1` | none (a name check, see below) | No | Team A's first player's name. |
| 6 | `playerA1Id` | `team_a` | Yes | Team A's first player's OPEN player ID. |
| 7 | `playerA2` | none (a name check) | No | Team A's second player's name. Empty in singles (see Singles below). |
| 8 | `playerA2Id` | `team_a` | Doubles | Team A's second player's OPEN player ID. Empty in singles. |
| 9 | `playerB1` | none (a name check) | No | Team B's first player's name. |
| 10 | `playerB1Id` | `team_b` | Yes | Team B's first player's OPEN player ID. |
| 11 | `playerB2` | none (a name check) | No | Team B's second player's name. Empty in singles. |
| 12 | `playerB2Id` | `team_b` | Doubles | Team B's second player's OPEN player ID. Empty in singles. |
| 13, 14 | `teamAGame1`, `teamBGame1` | `games[0]` | Yes | Game 1's points for Team A and Team B. |
| 15, 16 | `teamAGame2`, `teamBGame2` | `games[1]` | No | Game 2. |
| 17, 18 | `teamAGame3`, `teamBGame3` | `games[2]` | No | Game 3. |
| 19, 20 | `teamAGame4`, `teamBGame4` | `games[3]` | No | Game 4. |
| 21, 22 | `teamAGame5`, `teamBGame5` | `games[4]` | No | Game 5. |

`matchType` and `scoreType` are compared without regard to case or spaces around them, and must use only ASCII characters.

`matchType` is the match's format (singles or doubles). It is not OPEN's Match Type (tournament, league, and so on). The Event chosen at upload sets the Match Type.

## The header

- The header must be the first row that is not blank, with the 22 columns in the order above. Blank rows before it are skipped.
- Header names are compared without regard to case or spaces around them. They must use only ASCII characters.
- Each player ID column header may have any letters, digits, or underscores between the slot and `Id`. For example `playerA1Id`, `playerA1ExternalId`, and `playerA1RatingId` are all accepted in column 6. So the header from another system's export works unchanged.
- Empty cells after the last header column are ignored.
- A header that does not match is refused with `bad_header`. The message names the column number and the expected column, never the text in the file.

## Player IDs and names

- Each player ID column holds an OPEN player ID, such as `P-Z06Z-RMX0`. Case, spaces, and hyphens do not matter, and the `P-` prefix may be left out.
- Mapping another system's player IDs to OPEN players is not supported yet (decision F13 in #1).
- A player ID that is not a valid OPEN player ID fails the row with `unknown_player`. The upload (#71) also fails a row whose ID is valid but belongs to no player, or to a player whose account is closed, with the same code. The report names the row and the ID column (such as `playerB1Id`). It never repeats the ID itself.
- A name with no player ID fails the row with `missing_player_id`.
- Names never find or create a player. They are only a check. When a name does not match the player's display name, the upload's report shows a warning and the row still counts (#71).
- In singles, fill the first player of each team and leave the second empty (both the name and the ID). A team whose only player is in the second slot is also accepted.

## Dates

- Enter the date played as `YYYY-MM-DD`, such as `2026-04-11`. Other forms, such as `04/11/2026` or `20260411`, fail with `invalid_date`.
- A date in the future fails with `future_date`.
- The upload (#71) also fails a row dated before its Event's start date or after its end date with `outside_event_dates`.

## Scores

- Each score is a whole number made of the digits 0 to 9, such as `11` or `9`, with at most 9 digits. A longer one fails with `invalid_score`. No plus signs, decimals, spaces inside, or other characters: these fail with `invalid_score`, as does `-0`. A minus sign before another number is read, and the row then fails with `negative_score`.
- Enter games in order from game 1. Leave the cells of games not played empty. Empty cells at the end of a row may also be left out entirely.
- Both teams' cells of a played game must be filled.
- A game is won at the game target by 2 or more points, or past the target by exactly 2 points (for example 11-9, 11-4, or 13-11 with a target of 11).

## Values set once per upload

The file does not carry these. The upload (#71) sets them for the whole file.

- **Event.** The organizer picks the Event for the upload. The Event sets the Match Type. The `event` column is stored only as the event label.
- **Location.** Optional, a place's name such as a court or park.
- **Game target.** 11, 15, or 21 points. The default is 11.

## Values OPEN works out

- **Best of.** OPEN infers best of 1, 3, or 5 from the games, with the same rule as an API submission: the smallest best of whose winning number of games the last game reaches, with no game after it. For example 11-5 is best of 1, 11-5, 9-11, 11-6 is best of 3, and 11-5, 11-5, 11-5 is best of 5. A row whose games decide no best of 1, 3, or 5 fails with `cannot_infer`.
- **Order of play.** Row order is the order of play. Each row's position among the match rows is stored as the match's `sequence`, so matches on the same day replay in that order. Blank rows are skipped and take no number. A row that fails still takes its number, so the numbers do not depend on which rows pass.

## Players 13 or older

Every player in the file must be 13 or older (ADR 0009, #114). The file has no age column. Instead, the upload form asks the organizer once per file to confirm that every player in it is 13 or older (#71). An upload without the confirmation is refused before any row is read.

## File rules

- Save the file as UTF-8, with or without a byte order mark. Other encodings are refused with `not_utf8`.
- Use standard CSV quoting. A name with a comma or a quote goes in double quotes, such as `"O'Nealy, Morgan"`. Apostrophes need no quoting.
- Line endings may be CRLF (Windows) or LF. A trailing newline is optional.
- Blank rows are skipped.
- A file that is not valid CSV is refused with `malformed`: a quote that is never closed, a character right after a closing quote (such as `"Morgan"x`), or a NUL byte. A quote inside a cell that does not start with a quote (such as `Mo"rgan`) is read as part of the cell.
- A file with a header and no match rows is refused with `no_rows`.
- A file is at most 1 MiB (1,048,576 bytes) and at most 1000 match rows. A larger file is refused when it is sent. A file with more rows fails as a whole with `too_many_rows`, and no row is recorded.

## The report

The parser returns one result per match row, in file order. A row that passes has its normalized values. A row that fails has its issues, each with the columns it concerns (canonical names such as `playerB1Id`), a code, and a message. One bad row never stops the rows after it.

A failed row reports every cell issue and the match rule issues that can still be checked. Two limits apply:

- Only the games before the first bad game are checked against the match rules. Issues on later games, and whether the games decide the match, are not reported. For example, games `abc`-5 then 11-11 report only game 1's `invalid_score`, and the tie in game 2 shows up only after game 1 is fixed.
- When `matchType` is bad, the team size check does not run. For example, `matchType` `X` with one player on Team A reports only `invalid_choice`.

Each result names its row by its CSV record number. Records count from the start of the file, blank ones included, so when the header is the first record it is row 1, and the row numbers match a spreadsheet's for an ordinary file. A quoted name or event that spans more than one line is one record, so the rows after it are numbered one less per extra line.

Messages are fixed text. They name columns, game numbers, and allowed values, and never repeat what the file holds. The upload (#71) adds its own per-row results (player lookups, name warnings, duplicates) to the report. Only the good rows are recorded: there is no all-or-nothing mode yet (ADR 0036).

The upload's report gives each row one result:

- `recorded`, with the new match's OPEN Code. A `name_mismatch` warning on a name column means the name differs from the player's display name. A `possible_duplicate` warning means the match is held from counting until review, because another match has the same two teams and date with other scores.
- `duplicate`, when the same match is already recorded, or waits for confirmation. The report gives its OPEN Code when you may know it. The upload never confirms a waiting match.
- `failed`, with each reason. Besides the codes below, `forbidden` means you may not submit for this Event, and `voided` means the row's match was voided.

An upload of more than 100 match rows (by default, `UNUSUAL_SUBMISSION_BATCH_SIZE`), counting every row, failed ones included, has each of its matches flagged as an unusual submission. So are an Organizer's first matches after it is verified and a submitter's matches past a daily number (ADR 0049), and a match that takes one of its players past a daily number of matches on its date (`UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES`, 30 by default, ADR 0052). These flags are signals only: they do not stop a match counting, and the report does not show them.

A whole file can also fail: `too_many_rows`, the file errors above, `expired` when it could not be processed within three days, or `voided` when a moderator voided the upload or the uploader's matches. The rows processed before it stopped stay in the report as they were then: after a void, a row can still read `recorded` though its match is voided.

## Common errors

Each example starts from a valid doubles row and changes one thing.

| Problem | Example | Code | Columns |
| --- | --- | --- | --- |
| Unknown `scoreType` | `scoreType` is `TIMED` | `invalid_choice` | `scoreType` |
| Unknown `matchType` | `matchType` is `X` | `invalid_choice` | `matchType` |
| Match not decided | Games 11-5 and 9-11 only | `cannot_infer` | the game columns |
| Game after the match was decided | Games 11-5, 11-5, then 5-11 | `cannot_infer` | the game columns |
| Unknown player ID | `playerB1Id` holds an ID that is not an OPEN player ID | `unknown_player` | `playerB1Id` |
| Name without an ID | `playerA2` has a name, `playerA2Id` is empty | `missing_player_id` | `playerA2Id` |
| Same player twice on a team | `playerA1Id` and `playerA2Id` hold the same ID | `duplicate_player` | `playerA1Id`, `playerA2Id` |
| Same player on both teams | `playerA1Id` and `playerB1Id` hold the same ID | `player_on_both_teams` | `playerB1Id`, `playerB2Id` |
| Invalid score | A score of `abc`, `+9`, or `9.5` | `invalid_score` | that game's two columns |
| Negative score | A score of `-3` | `negative_score` | that game's two columns |
| Tied game | 11-11 | `tie` | that game's two columns |
| Winner short of the target | 9-7 with a target of 11 | `target_not_reached` | that game's two columns |
| Game played past the target by more than 2 | 15-10 with a target of 11 | `played_past_target` | that game's two columns |
| Only one score in a game | `teamAGame3` filled, `teamBGame3` empty | `incomplete_game` | that game's two columns |
| A game skipped | Game 2 empty, game 3 filled | `missing_game` | game 2's columns |
| No games | Every score cell empty | `no_games` | the game columns |
| Future date | A date after today | `future_date` | `date` |
| Date outside the Event | A date after the Event's last day | `outside_event_dates` | `date` |
| Wrong date form | `2026/04/11` or `2026-02-30` | `invalid_date` | `date` |
| Wrong team size for `matchType` | `D` with one player a side, or `S` with two | `team_size` | the ID columns of that team that hold a player (both of the team's ID columns when it has none) |
| Event label too long | More than 200 characters | `too_long` | `event` |
| Cells past the last column | A note in column 23 | `extra_columns` | none (the whole row) |

## How the upload works (#71)

The upload is `apps/matches/results_files.py` (ADR 0036). It does what this section asks, records each row as one match of the upload's Submission Batch, and reports an exact duplicate of an existing match as `duplicate`, a pending one included: an upload never confirms a pending match.

- Read files only through `apps.matches.csv_format.parse_results_csv(data, game_target=...)`. It raises `CsvFileError` (with `code` and `message`) for a file it cannot read, and `ValueError` for a game target outside 11, 15, and 21.
- Each `CsvMatch` holds the row number, `sequence`, format, scoring type, event label, date, both teams as `CsvPlayer` (ID column, name, normalized OPEN Code), games, inferred best of, and game target. Look up each code, report an unknown one as `unknown_player` on its column, and compare each name with the player's display name as a warning.
- Record each row with `record_match()` in its own transaction, passing `sequence`, `event_label`, and the upload's Event and location.
- A parsed `CsvMatch` can still be refused by `record_match()`: the model's limits (such as the largest score a Game stores), the submitter's permission, or a duplicate. Handle `MatchValidationError`, `DuplicateMatchError`, and `MatchPermissionDenied` (and `record_match()`'s batch refusal, a `ValueError`, as `forbidden`) for each row and report them on that row.
- The parser does not check players' ages, file size, or row count. The upload checks the size and row count, and asks the organizer to confirm every player is 13 or older.
- Change this page, the template, and the parser together.

"""CSV results uploads (#71): store a Results File, process it, report each row.

An organizer uploads a CSV results file for one Event
(``create_results_file``). The request stores the file with a new
Submission Batch (the uploader and the Event) and one Audit Event, and
queues ``apps.matches.tasks.process_results_file`` after commit.

The task (``process_results_file``) reads the file only through
``apps.matches.csv_format.parse_results_csv``, looks up every player ID,
and records each good row with ``record_match()`` as the uploader, each
call in its own transaction, so ``record_match()`` checks the
uploader's permission, the batch, and duplicates on every row. Only the
good rows are recorded: there is no all-or-nothing mode yet (ADR 0036).
A batch row never confirms a pending match: an exact duplicate of one is
reported as a duplicate (ADR 0036).

The report (``ResultsFile.report``) holds one result per match row: the
row number, the sequence, the result (``recorded``, ``duplicate``, or
``failed``), the Match's OPEN Code when there is one the uploader may
know, and issues and warnings as columns, code, and fixed message. It
never holds a name, a player ID, or any other cell value. The raw file
is emptied once processing ends (done or failed), and the sweep
(``sweep_results_files``) fails a file still pending after
``PENDING_EXPIRY`` and empties it.

Processing is safe to run twice: a row whose match this batch already
recorded with the same sequence is reported as recorded again, and the
report is stored only while the file is still pending.

Nothing here reads payment status (CLAUDE.md, hard rule 7). Nothing is
logged but row ids and error classes: the file holds names and IDs.
"""

import logging
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from django.db import transaction
from django.utils import timezone
from kombu.exceptions import OperationalError

from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.events.models import Event
from apps.matches.csv_format import (
    UNKNOWN_PLAYER_MESSAGE,
    CsvFileError,
    CsvIssue,
    CsvMatch,
    CsvPlayer,
    CsvRowError,
    match_issue_columns,
    name_column,
    parse_results_csv,
)
from apps.matches.models import (
    FlagReason,
    GameTarget,
    Match,
    MatchFlag,
    ResultsFile,
    ResultsFileStatus,
    SubmissionBatch,
)
from apps.matches.permissions import MatchPermissionDenied, can_manage_events
from apps.matches.services import (
    BATCH_REFUSED,
    DuplicateMatchError,
    MatchValidationError,
    record_match,
    share_lock_event,
)
from apps.players.models import Player

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

logger = logging.getLogger(__name__)

# The largest file the upload accepts, in bytes, and the most match rows.
MAX_FILE_BYTES: Final = 1_048_576
MAX_ROWS: Final = 1000
# A file still pending this long after upload fails as ``expired``.
PENDING_EXPIRY: Final = timedelta(days=3)
# A pending file queued this long ago is queued again by the sweep. Longer
# than the task's hard time limit, so a run still going is never doubled.
REQUEUE_AFTER: Final = timedelta(minutes=35)

AGE_CONFIRMATION_REQUIRED: Final = (
    "Confirm that every player in the file is 13 or older."
)
FILE_TOO_LARGE: Final = "The file is too large."

TOO_MANY_ROWS: Final = "too_many_rows"
EXPIRED: Final = "expired"

NAME_MISMATCH_MESSAGE: Final = "This name does not match the player's display name."
POSSIBLE_DUPLICATE_MESSAGE: Final = (
    "This match may repeat an earlier match. It is held for review."
)

# What publishing a task raises when the broker cannot take it: kombu's
# connection error, or a socket error (ConnectionError is one).
_BROKER_ERRORS = (OperationalError, OSError)


class ResultRow(StrEnum):
    """The values of ``RowResult.result``."""

    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class ResultsFileFailed(Exception):
    """Processing a Results File failed in a worker.

    Fixed text: only the Results File's row id and the error's class, never
    the original error's text, which could quote the file.
    """

    def __init__(self, results_file_id: int, error_class: str) -> None:
        super().__init__(
            f"Processing results file {results_file_id} failed ({error_class})."
        )


@dataclass(frozen=True)
class ReportIssue:
    """One issue or warning on a row: columns, code, fixed message."""

    columns: list[str]
    code: str
    message: str


@dataclass(frozen=True)
class RowResult:
    """One match row's result, as stored in ``ResultsFile.report``."""

    row: int
    sequence: int
    result: ResultRow
    match: str | None = None
    issues: list[ReportIssue] = field(default_factory=list)
    warnings: list[ReportIssue] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        values = asdict(self)
        values["result"] = self.result.value
        return values


def _issue(issue: CsvIssue) -> ReportIssue:
    return ReportIssue(list(issue.columns), issue.code, issue.message)


# Store


def _enqueue(results_file_id: int) -> None:
    """Queue processing. A broker error is logged by id and class only; the
    sweep queues the file again."""
    from apps.matches.tasks import process_results_file

    try:
        process_results_file.delay(results_file_id)
    except _BROKER_ERRORS as exc:
        logger.error(
            "Could not queue results file %s (%s).",
            results_file_id,
            type(exc).__name__,
        )


def create_results_file(
    *,
    account: "_User",
    event: Event,
    content: bytes,
    game_target: int,
    location: str,
    players_13_or_older: bool,
) -> ResultsFile:
    """Store an uploaded results file for ``event`` and queue its processing.

    Raises ``ValueError`` (fixed text) when ``players_13_or_older`` is not
    True or the file is larger than ``MAX_FILE_BYTES``, Django's
    ``ValidationError`` for a game target or location the model refuses,
    and ``MatchPermissionDenied`` when ``account`` may not manage the
    Event's Organizer's Events (``can_manage_events``), checked under the
    Event row's ``FOR KEY SHARE`` lock, so an Event delete or organizer
    change waits for this upload (ADR 0035). Nothing is written in any of
    these cases.

    In one transaction it creates the Submission Batch (submitter the
    Account, the Event, no Club), the Results File, and one
    ``results_file.create`` Audit Event, whose values are the Event's
    OPEN Code, the game target, and the age confirmation, never the file
    or the location. The task is queued after commit with the row id only.
    """
    if players_13_or_older is not True:
        raise ValueError(AGE_CONFIRMATION_REQUIRED)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(FILE_TOO_LARGE)
    if event.pk is None:
        raise MatchPermissionDenied
    results_file = ResultsFile(
        content=content, game_target=game_target, location=location
    )
    results_file.full_clean(exclude=["public_code", "submission_batch"])
    with transaction.atomic():
        share_lock_event(event.pk)
        stored = (
            Event.objects.filter(pk=event.pk)
            .values_list("organizer_id", "public_code")
            .first()
        )
        if stored is None or not can_manage_events(account, stored[0]):
            raise MatchPermissionDenied
        batch = SubmissionBatch.objects.create(
            submitted_by_user=account, event_id=event.pk
        )
        results_file.submission_batch = batch
        results_file.save()
        record_event(
            action=AuditAction.RESULTS_FILE_CREATE,
            target=results_file,
            actor=account,
            before=None,
            after={
                "event": stored[1],
                "game_target": int(results_file.game_target),
                "players_13_or_older": True,
            },
        )
        # Not robust: a robust callback would swallow any other error and
        # log its text.
        transaction.on_commit(partial(_enqueue, results_file.pk))
    return results_file


# Process


def _name_key(name: str) -> str:
    """A name compared without regard to case, width, or spacing."""
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def _players(rows: tuple[CsvMatch | CsvRowError, ...]) -> dict[str, Player]:
    """Every Player the rows name, by OPEN Code, in one query.

    A Player whose Account is inactive is left out, so it counts as
    unknown, as the API's player lookup treats it.
    """
    codes = {
        player.code
        for row in rows
        if isinstance(row, CsvMatch)
        for player in (*row.team_a, *row.team_b)
    }
    return {
        player.public_code: player
        for player in Player.objects.filter(
            public_code__in=codes, account__is_active=True
        )
    }


def _held(match_id: int) -> bool:
    return MatchFlag.objects.filter(
        match_id=match_id,
        reason=FlagReason.POSSIBLE_DUPLICATE,
        resolution="",
    ).exists()


def _recorded(row: CsvMatch, match: Match, warnings: list[ReportIssue]) -> RowResult:
    if _held(match.pk):
        warnings = [
            *warnings,
            ReportIssue(
                [], FlagReason.POSSIBLE_DUPLICATE.value, POSSIBLE_DUPLICATE_MESSAGE
            ),
        ]
    return RowResult(
        row.row,
        row.sequence,
        ResultRow.RECORDED,
        match=match.public_code,
        warnings=warnings,
    )


def _record_row(
    row: CsvMatch,
    players: dict[str, Player],
    *,
    results_file: ResultsFile,
    batch: SubmissionBatch,
    uploader: "_User | None",
    event: Event | None,
) -> RowResult:
    """Record one parsed row, or say why not."""
    unknown = [
        player for player in (*row.team_a, *row.team_b) if player.code not in players
    ]
    if unknown:
        return RowResult(
            row.row,
            row.sequence,
            ResultRow.FAILED,
            issues=[
                ReportIssue([player.column], "unknown_player", UNKNOWN_PLAYER_MESSAGE)
                for player in unknown
            ],
        )
    warnings = [
        ReportIssue([name_column(player)], "name_mismatch", NAME_MISMATCH_MESSAGE)
        for player in (*row.team_a, *row.team_b)
        if player.name
        and _name_key(player.name) != _name_key(players[player.code].display_name)
    ]

    def team(members: tuple[CsvPlayer, ...]) -> list[Player]:
        return [players[member.code] for member in members]

    def failed(issues: list[ReportIssue]) -> RowResult:
        return RowResult(
            row.row, row.sequence, ResultRow.FAILED, issues=issues, warnings=warnings
        )

    try:
        recorded = record_match(
            format=row.format,
            team_a=team(row.team_a),
            team_b=team(row.team_b),
            games=row.games,
            played_on=row.played_on,
            best_of=row.best_of,
            game_target=row.game_target,
            scoring_type=row.scoring_type,
            submitted_by_user=uploader,
            event=event,
            location=results_file.location,
            event_label=row.event_label,
            sequence=row.sequence,
            submission_batch=batch,
        )
    except DuplicateMatchError as error:
        earlier = (
            Match.objects.filter(
                public_code=error.existing_code,
                submission_batch_id=batch.pk,
                sequence=row.sequence,
            ).first()
            if error.existing_code is not None
            else None
        )
        if earlier is not None:
            # This row, recorded by an earlier run of this file.
            return _recorded(row, earlier, warnings)
        return RowResult(
            row.row,
            row.sequence,
            ResultRow.DUPLICATE,
            match=error.existing_code,
            issues=[ReportIssue([], error.kind.value, error.message)],
            warnings=warnings,
        )
    except MatchValidationError as error:
        return failed(
            [
                ReportIssue(
                    list(match_issue_columns(issue, row)), issue.code, issue.message
                )
                for issue in error.issues
            ]
        )
    except MatchPermissionDenied as error:
        return failed([ReportIssue([], "forbidden", error.message)])
    except ValueError as error:
        if str(error) != BATCH_REFUSED:
            raise
        return failed([ReportIssue([], "forbidden", MatchPermissionDenied.message)])
    return _recorded(row, recorded.match, warnings)


def _finish(
    results_file_id: int, *, report: list[RowResult], error_code: str = ""
) -> None:
    """Store the report and empty the file, once, while it is still pending."""
    with transaction.atomic():
        results_file = (
            ResultsFile.objects.select_for_update().filter(pk=results_file_id).first()
        )
        if results_file is None or results_file.status != ResultsFileStatus.PENDING:
            return
        results_file.report = [result.as_json() for result in report]
        results_file.status = (
            ResultsFileStatus.FAILED if error_code else ResultsFileStatus.DONE
        )
        results_file.error_code = error_code
        results_file.content = b""
        results_file.processed_at = timezone.now()
        results_file.save(
            update_fields=[
                "report",
                "status",
                "error_code",
                "content",
                "processed_at",
            ]
        )


def process_results_file(results_file_id: int) -> None:
    """Read a pending Results File, record its good rows, store the report.

    Does nothing for a missing file or one no longer pending. A file the
    parser cannot read fails with the parser's code, and one with more
    than ``MAX_ROWS`` match rows fails with ``too_many_rows``, before any
    row is recorded. Each row is recorded in its own transaction: never
    call this inside an outer ``transaction.atomic()``, since
    ``record_match()``'s advisory locks last until the outermost
    transaction ends. Any other error propagates and leaves the file
    pending, for the sweep to queue again.
    """
    results_file = (
        ResultsFile.objects.select_related(
            "submission_batch__event", "submission_batch__submitted_by_user"
        )
        .filter(pk=results_file_id)
        .first()
    )
    if results_file is None or results_file.status != ResultsFileStatus.PENDING:
        return
    batch = results_file.submission_batch
    try:
        parsed = parse_results_csv(
            bytes(results_file.content),
            game_target=GameTarget(results_file.game_target),
        )
    except CsvFileError as error:
        _finish(results_file_id, report=[], error_code=error.code)
        return
    if len(parsed.rows) > MAX_ROWS:
        _finish(results_file_id, report=[], error_code=TOO_MANY_ROWS)
        return
    players = _players(parsed.rows)
    report: list[RowResult] = []
    for row in parsed.rows:
        if isinstance(row, CsvRowError):
            report.append(
                RowResult(
                    row.row,
                    row.sequence,
                    ResultRow.FAILED,
                    issues=[_issue(issue) for issue in row.issues],
                )
            )
            continue
        report.append(
            _record_row(
                row,
                players,
                results_file=results_file,
                batch=batch,
                uploader=batch.submitted_by_user,
                event=batch.event,
            )
        )
    _finish(results_file_id, report=report)


def sweep_results_files() -> int:
    """Fail stale pending files and queue lost ones again.

    A file still pending ``PENDING_EXPIRY`` after upload fails as
    ``expired`` and is emptied. A pending file last queued more than
    ``REQUEUE_AFTER`` ago is queued again after commit, and its queue time
    is set, so later sweeps skip it while that run may still be going.
    Returns how many files were queued.
    """
    now = timezone.now()
    for results_file_id in ResultsFile.objects.filter(
        status=ResultsFileStatus.PENDING, created_at__lt=now - PENDING_EXPIRY
    ).values_list("pk", flat=True):
        _finish(results_file_id, report=[], error_code=EXPIRED)
    queued = 0
    stale = ResultsFile.objects.filter(
        status=ResultsFileStatus.PENDING, queued_at__lt=now - REQUEUE_AFTER
    )
    for results_file_id in stale.values_list("pk", flat=True):
        with transaction.atomic():
            if stale.filter(pk=results_file_id).update(queued_at=now):
                transaction.on_commit(partial(_enqueue, results_file_id))
                queued += 1
    return queued

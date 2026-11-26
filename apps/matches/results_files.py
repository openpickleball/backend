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
``PENDING_EXPIRY``, once no run can still hold it, and empties it.

Each row runs in its own transaction, in this lock order: the Results
File row (``FOR UPDATE``), then, inside ``record_match()``, the Event row
(``FOR KEY SHARE``) and the advisory locks. Under the file lock the row
stops the whole run when the file is no longer pending, looks for a
Match of this batch with the row's sequence in any status (a voided one
included), calls ``record_match()`` only when there is none, and stores
the row's result in the file's report, keyed by sequence. So results
survive a run that stops, and a rerun never records a row twice: a row
whose match exists is reported as recorded, or as ``voided``, with the
voided match's code, when it was voided (by a moderator or a
correction).

A moderator's bulk void of the batch (``void_submission_batch``) first
fails a pending file as ``voided`` (``fail_for_void``) under the same
file lock, keeping the row results stored so far, and only then voids
the matches. A void of every match of the uploader
(``void_submitter_matches``) does the same for each of the uploader's
pending files first (``fail_for_submitter_void``). A run in flight either
committed a row before that (the void sees its match) or sees the file
failed at its next row and stops.

A failed file's stored row results describe the state when processing
stopped. A bulk void fails the file and then voids the rows' matches, so
those rows still read ``recorded``: the file's ``voided`` error is what
tells the uploader.

Before the first row, under the file lock, processing sets the batch's
``expected_size`` to the file's number of match rows (#206), once: a
rerun keeps the size an earlier run stored. So when the upload is larger
than ``UNUSUAL_SUBMISSION_BATCH_SIZE``, every recorded match gets a
signal-only ``large_batch`` flag from the first row; the matches still
count (ADR 0049). The lock order is the file row, then the batch row.

Processing is safe to run twice, even at once: runs take the file lock
row by row, and the final report is stored only while the file is still
pending.

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
    MatchParticipant,
    MatchStatus,
    ResultsFile,
    ResultsFileStatus,
    SubmissionBatch,
)
from apps.matches.notices import schedule_batch_notices
from apps.matches.permissions import MatchPermissionDenied, can_manage_events
from apps.matches.services import (
    BATCH_REFUSED,
    DuplicateMatchError,
    MatchValidationError,
    share_lock_event,
)
from apps.matches.services import (
    record_match as record_match,  # Patched by tests.
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
# than the task's hard time limit, so a run that started when it was
# queued has ended. A run that waited in the queue first can still overlap
# a copy queued again; that is safe (see the module docstring).
REQUEUE_AFTER: Final = timedelta(minutes=35)

AGE_CONFIRMATION_REQUIRED: Final = (
    "Confirm that every player in the file is 13 or older."
)
FILE_TOO_LARGE: Final = "The file is too large."

TOO_MANY_ROWS: Final = "too_many_rows"
EXPIRED: Final = "expired"
VOIDED: Final = "voided"

VOIDED_MESSAGE: Final = "This row's match was voided."

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


def _recorded(row: CsvMatch, match: Match, warnings: list[ReportIssue]) -> RowResult:
    return RowResult(
        row.row,
        row.sequence,
        ResultRow.RECORDED,
        match=match.public_code,
        warnings=warnings,
    )


def _mark_held(report: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Warn on every recorded row whose match is confirmed but held.

    Takes and returns stored row results (``RowResult.as_json()``). Read
    when processing ends, from ``Match.objects.counted()``, so both
    matches of a held pair are warned about and a match whose hold was
    lifted (the other match voided, or the flag resolved) is not. Any
    earlier ``possible_duplicate`` warning is replaced, so it applies once.
    """
    code = FlagReason.POSSIBLE_DUPLICATE.value
    codes = [
        stored["match"]
        for stored in report
        if stored.get("result") == ResultRow.RECORDED and stored.get("match")
    ]
    held = set(
        Match.objects.filter(public_code__in=codes, status=MatchStatus.CONFIRMED)
        .exclude(pk__in=Match.objects.counted().values("pk"))
        .values_list("public_code", flat=True)
    )
    warning = asdict(ReportIssue([], code, POSSIBLE_DUPLICATE_MESSAGE))
    marked: list[dict[str, Any]] = []
    for stored in report:
        warnings = [w for w in stored.get("warnings", []) if w.get("code") != code]
        if stored.get("result") == ResultRow.RECORDED and stored.get("match") in held:
            warnings.append(warning)
        marked.append({**stored, "warnings": warnings})
    return marked


def _name_warnings(row: CsvMatch, display_names: dict[str, str]) -> list[ReportIssue]:
    """A ``name_mismatch`` warning for each non-blank name that differs from
    the display name of the player its ID names (by OPEN Code)."""
    return [
        ReportIssue([name_column(player)], "name_mismatch", NAME_MISMATCH_MESSAGE)
        for player in (*row.team_a, *row.team_b)
        if player.name
        and player.code in display_names
        and _name_key(player.name) != _name_key(display_names[player.code])
    ]


def _record_row(
    row: CsvMatch,
    players: dict[str, Player],
    *,
    location: str,
    batch: SubmissionBatch,
    uploader: "_User | None",
    event: Event | None,
) -> RowResult:
    """Record one parsed row, or say why not. Call it under the file lock,
    with no match of this batch at the row's sequence."""
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
    warnings = _name_warnings(
        row, {code: player.display_name for code, player in players.items()}
    )

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
            location=location,
            event_label=row.event_label,
            sequence=row.sequence,
            submission_batch=batch,
        )
    except DuplicateMatchError as error:
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
    results_file_id: int,
    *,
    report: list[RowResult] | None,
    error_code: str = "",
) -> int | None:
    """End processing, once, while the file is still pending.

    Returns the file's Submission Batch id when this call ended it, else
    None.

    Sets the status (failed when ``error_code`` is set, else done), empties
    the file, and stores ``report``, or keeps the row results already
    stored when ``report`` is None. Either way the held-match warning is
    applied to the stored rows (``_mark_held``). The stored rows describe
    the state at this moment: nothing updates them afterward.

    Unless a bulk void stopped the file (``VOIDED``), it schedules the
    batch's recorded match notices after commit (``schedule_batch_notices``,
    #208): one email per player for the whole upload. It runs once per
    file, since only the call that finds the file pending gets this far.
    A bulk void schedules them itself, once it has voided the matches.
    """
    with transaction.atomic():
        results_file = (
            ResultsFile.objects.select_for_update()
            .defer("content")
            .filter(pk=results_file_id)
            .first()
        )
        if results_file is None or results_file.status != ResultsFileStatus.PENDING:
            return None
        if report is not None:
            results_file.report = [result.as_json() for result in report]
        results_file.report = _mark_held(results_file.report)
        results_file.status = (
            ResultsFileStatus.FAILED if error_code else ResultsFileStatus.DONE
        )
        results_file.error_code = error_code
        results_file.content = b""
        results_file.processed_at = timezone.now()
        results_file.save(
            update_fields=["report", "status", "error_code", "content", "processed_at"]
        )
        if error_code != VOIDED:
            schedule_batch_notices(results_file.submission_batch_id)
        return results_file.submission_batch_id


def fail_for_void(batch_id: int) -> list[int]:
    """Fail the batch's Results File as ``voided``, if it is still pending.

    ``apps.matches.workflow.void_submission_batch`` calls it, in its own
    transaction, before it reads or voids any match, so no run records a
    row of the file afterward. The row results stored so far are kept as
    they are: the void then voids their matches, so they still read
    ``recorded``, and the file's ``voided`` error tells the uploader.

    Returns the batch ids of the files it failed (``[batch_id]`` or
    none), whose recorded match notices the bulk void sends afterward.
    """
    results_file_id = (
        ResultsFile.objects.filter(submission_batch_id=batch_id)
        .values_list("pk", flat=True)
        .first()
    )
    if results_file_id is None:
        return []
    ended = _finish(results_file_id, report=None, error_code=VOIDED)
    return [] if ended is None else [ended]


def fail_for_submitter_void(
    *, user_id: int | None = None, application_id: int | None = None
) -> list[int]:
    """Fail every pending Results File of a submitter as ``voided``.

    ``apps.matches.workflow.void_submitter_matches`` calls it before it
    reads or voids any match. Each file is failed in its own short
    transaction, under its row lock (``_finish``), keeping its stored row
    results, so no run of it records a row afterward. Returns the batch
    ids of the files it failed, as ``fail_for_void`` does.
    """
    files = ResultsFile.objects.filter(status=ResultsFileStatus.PENDING)
    if user_id is not None:
        files = files.filter(submission_batch__submitted_by_user_id=user_id)
    elif application_id is not None:
        files = files.filter(
            submission_batch__submitted_by_application_id=application_id
        )
    else:
        return []
    failed = []
    for results_file_id in files.values_list("pk", flat=True):
        ended = _finish(results_file_id, report=None, error_code=VOIDED)
        if ended is not None:
            failed.append(ended)
    return failed


def _existing_result(row: CsvMatch, match: Match) -> RowResult:
    """The result of a row whose match this batch already holds.

    Its name warnings are worked out again from the match's own players,
    whatever their Accounts' state now, against their current display
    names. So the result matches the one the row got when it was recorded
    unless a player's display name changed in between, which changes the
    warning.
    """
    display_names = dict(
        MatchParticipant.objects.filter(match_id=match.pk).values_list(
            "player__public_code", "player__display_name"
        )
    )
    warnings = _name_warnings(row, display_names)
    if match.status == MatchStatus.VOIDED:
        return RowResult(
            row.row,
            row.sequence,
            ResultRow.FAILED,
            match=match.public_code,
            issues=[ReportIssue([], VOIDED, VOIDED_MESSAGE)],
            warnings=warnings,
        )
    return _recorded(row, match, warnings)


def _store(results_file: ResultsFile, result: RowResult) -> None:
    """Put ``result`` in the file's stored report, in place of any result
    with the same sequence."""
    report = [
        stored
        for stored in results_file.report
        if stored.get("sequence") != result.sequence
    ]
    report.append(result.as_json())
    report.sort(key=lambda stored: int(stored["sequence"]))
    results_file.report = report
    results_file.save(update_fields=["report"])


def _process_row(
    results_file_id: int,
    row: CsvMatch | CsvRowError,
    players: dict[str, Player],
    *,
    batch: SubmissionBatch,
    location: str,
) -> RowResult | None:
    """Process one row under the file's lock and store its result.

    Returns None, writing nothing, when the file is no longer pending: the
    run stops.
    """
    with transaction.atomic():
        results_file = (
            ResultsFile.objects.select_for_update()
            .defer("content")
            .filter(pk=results_file_id)
            .first()
        )
        if results_file is None or results_file.status != ResultsFileStatus.PENDING:
            return None
        if isinstance(row, CsvRowError):
            result = RowResult(
                row.row,
                row.sequence,
                ResultRow.FAILED,
                issues=[_issue(issue) for issue in row.issues],
            )
        else:
            existing = (
                Match.objects.filter(
                    submission_batch_id=batch.pk, sequence=row.sequence
                )
                .order_by("pk")
                .first()
            )
            if existing is not None:
                result = _existing_result(row, existing)
            else:
                result = _record_row(
                    row,
                    players,
                    location=location,
                    batch=batch,
                    uploader=batch.submitted_by_user,
                    event=batch.event,
                )
        _store(results_file, result)
    return result


def _expect(results_file_id: int, batch: SubmissionBatch, size: int) -> bool:
    """Store the file's number of match rows as the batch's expected size.

    Runs before any row, under the file's lock and then the batch's row
    lock (inside ``set_expected_size``), so the large batch rule flags
    every match from the first row (#206). A size an earlier run stored
    stays: a rerun never changes it. Returns False, writing nothing, when
    the file is no longer pending: the run stops.
    """
    with transaction.atomic():
        pending = (
            ResultsFile.objects.select_for_update()
            .filter(pk=results_file_id, status=ResultsFileStatus.PENDING)
            .values_list("pk", flat=True)
        )
        if not pending:
            return False
        stored = (
            SubmissionBatch.objects.filter(pk=batch.pk)
            .values_list("expected_size", flat=True)
            .first()
        )
        if stored is None:
            batch.set_expected_size(size)
    return True


def process_results_file(results_file_id: int) -> None:
    """Read a pending Results File, record its good rows, store the report.

    Does nothing for a missing file or one no longer pending. A file the
    parser cannot read fails with the parser's code, and one with more
    than ``MAX_ROWS`` match rows fails with ``too_many_rows``, before any
    row is recorded. Then, before the first row, the batch's
    ``expected_size`` is set to the number of match rows, once (#206). Each row runs in its own transaction (see the module
    docstring): never call this inside an outer ``transaction.atomic()``,
    since ``record_match()``'s advisory locks last until the outermost
    transaction ends. The run stops at the first row that finds the file
    no longer pending. Any other error propagates and leaves the file
    pending, with the row results stored so far, for the sweep to queue
    again.
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
    if parsed.rows and not _expect(results_file_id, batch, len(parsed.rows)):
        return
    players = _players(parsed.rows)
    report: list[RowResult] = []
    for row in parsed.rows:
        result = _process_row(
            results_file_id,
            row,
            players,
            batch=batch,
            location=results_file.location,
        )
        if result is None:
            return
        report.append(result)
    _finish(results_file_id, report=report)


def sweep_results_files() -> int:
    """Fail stale pending files and queue lost ones again.

    A file still pending ``PENDING_EXPIRY`` after upload, and last queued
    more than ``REQUEUE_AFTER`` ago (so no run can still be going), fails
    as ``expired`` and is emptied, keeping the row results already
    stored. A pending file last queued more than
    ``REQUEUE_AFTER`` ago is queued again after commit, and its queue time
    is set, so later sweeps skip it for ``REQUEUE_AFTER``.
    Returns how many files were queued.
    """
    now = timezone.now()
    for results_file_id in ResultsFile.objects.filter(
        status=ResultsFileStatus.PENDING,
        created_at__lt=now - PENDING_EXPIRY,
        queued_at__lt=now - REQUEUE_AFTER,
    ).values_list("pk", flat=True):
        _finish(results_file_id, report=None, error_code=EXPIRED)
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

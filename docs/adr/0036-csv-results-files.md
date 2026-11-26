# 0036. CSV results files

- Status: Proposed
- Date: 2026-11-25

## Context

Issue #71 lets a verified Organizer upload a CSV results file for one Event (format in `docs/csv-format.md`). Three questions were open:

- ADR 0011 left #71 to decide how an all-or-nothing upload mode gets its result, since each match must be recorded in its own transaction and a concurrent submission can make a later row a duplicate after earlier rows have committed.
- ADR 0032 left #71 and #72 to decide whether a batch submission may confirm a pending match it exactly duplicates (ADR 0013). Such a confirmation is not linked to the batch, so a moderator's bulk void does not undo it.
- The file holds player names and IDs, which must not travel through the broker or the worker log, and the organizer needs a report of every row.

The project owner decided all three on 2026-11-25 (comment on issue #71).

## Decision

- **Valid rows only.** An upload records every row that passes and reports the rest. There is no all-or-nothing mode for now. A later issue can add one; it must still record each match in its own transaction (ADR 0011).
- **A batch never confirms a pending match.** `record_match()` called with a `submission_batch` treats an exact duplicate of a pending match like any other exact duplicate: it raises `DuplicateMatchError` and writes nothing. The upload reports the row as a duplicate. This is an exception to ADR 0013's duplicate path, which still applies to a single submission. So every match an upload changes is a match in its batch, and the bulk void reaches all of them.
- **A stored Results File.** The request stores the file on a `matches.ResultsFile` row, with its own OPEN Code (prefix `F`, 8 characters like an Event's, amending ADR 0008), a new Submission Batch (the uploader and the Event), and one `results_file.create` Audit Event (the Event's OPEN Code, the game target, and the uploader's confirmation that every player is 13 or older). It first takes `FOR KEY SHARE` on the Event row and checks the uploader's authority again, so a concurrent Event delete waits for the upload and then refuses (the batch's `PROTECT`). The task `process_results_file` on the `default` queue gets only the row's id.
- **Per row, in its own transaction.** Each row locks the Results File row, stops when the file is no longer pending, records the row with `record_match()` as the uploader (unless a match of this batch already has the row's sequence), and stores the row's result in the report. A rerun therefore never records a row twice, and a run that stops keeps the results it stored.
- **A bulk void stops the upload.** `void_submission_batch` first fails the batch's pending Results File (`voided`), and `void_submitter_matches` every pending Results File of that submitter, then each voids the matches, so a run in flight or a later rerun records nothing more.
- **The file is short-lived; the report is not.** The raw file is emptied when processing ends. A file still pending after three days is failed (`expired`) and emptied by a sweep that runs every 15 minutes and also queues stalled files again. The report holds row numbers, results, OPEN Codes, and fixed messages, never a name or a cell value, and is kept. Only the uploading Account reads it (`GET /api/v1/organizer/results-files/{code}`).
- **Limits.** A file is at most 1 MiB and 1000 match rows. A larger file is refused with 413. A request that declares a body over the file limit plus a 64 KiB allowance for the form is refused before Django reads it.

## Consequences

- An organizer who wants all or nothing fixes the failed rows and uploads them again; the recorded rows come back as duplicates.
- When an upload's row exactly duplicates a player's pending self-submission, the upload records nothing for that row and reports it as a duplicate. The pending match waits for the players' confirmation, or for the Organizer to send the same match on its own, outside an upload (ADR 0013's duplicate path).
- A failed file's report describes the rows as they stood when processing stopped. After a bulk void, its rows still read `recorded`; the file's `voided` error is what tells the uploader. A bulk void of a submitter stops every pending file of that submitter, whatever its date.
- An Event with any Results File cannot be deleted, even when every row failed, because its batch names it.
- A pending file holds other players' names and IDs for about three days at most (the sweep also waits until 35 minutes after the file was last queued), inside ADR 0009's 30 days for raw results files, so Account deletion needs no scrubbing of files.
- Uploads share the Account's API throttle (ADR 0030). Large uploads and an Organizer's first matches are flagged, not held, by #206 (ADR 0049).

## Alternatives considered

- Check every row first, then void the recorded rows if a later row fails: a void of counted matches moves ratings twice and needs an actor who may void them. Deferred with the mode.
- Let a batch confirm a pending duplicate and link it to the batch: the ledger guard keeps a Match's batch link fixed once written.
- Pass the file to the task: task arguments must not carry personal data (CLAUDE.md, Settings).

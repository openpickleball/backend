# 0032. Submission batches and moderator bulk void

- Status: Proposed
- Amended by: [ADR 0036](0036-csv-results-files.md) (a batch never confirms a pending duplicate, which closes the gap below; each bulk void first fails the pending Results Files in its scope)
- Amended by: [ADR 0034](0034-lowest-level-resolution.md) (a player's correction of a self-reported match has the correcting player as its submitter; #273 removed the single void action and `void_match`'s moderator path, so the bulk void now voids each match through its own moderator void, which applies `moderates` to the match and records the same `match.void` event)
- Date: 2026-11-24

## Context

A match from a trusted submitter counts as soon as it is recorded (ADR 0012, ADR 0021). A compromised or bad-faith submitter can record hundreds of matches in minutes through a CSV upload (#71) or a bulk request (#72). The Ledger and replay make recovery possible, but a moderator could void only the matches they picked by hand from the list (ADR 0029). Issue #205 asks for one action that voids them, with a way to tell which matches came in together. The batch paths themselves are not built yet.

## Decision

- **A Submission Batch record.** `matches.SubmissionBatch` holds exactly one submitter (an Account or a Partner Application), an optional Event and Club, and the time. A batch path creates one and passes it to `record_match(submission_batch=...)` for every match, which sets `Match.submission_batch`. A single submission leaves the link empty.
- **Set once.** The link is set only when the Match is created. The ledger guard refuses any later change, and a batch row is insert-only like the ledger rows: never changed or deleted. Every link uses `PROTECT`.
- **No OPEN Code.** No API shows a batch, so it has none. A later issue that shows batches in the API adds one.
- **A batch must fit its matches.** `record_match()` refuses a batch whose stored row does not exist, whose submitter is not exactly the match's submitter, or whose Event or Club, when set, is not the match's. It reads the batch from the database by id. A batch with no Event or Club accepts any, so a bulk request can span Events. The check runs after the permission checks and before validation. A refusal is a `ValueError` with fixed text: only code passes a batch, never a request.
- **Two bulk void services** in `apps.matches.workflow`:
  - `void_submission_batch` voids every match of one batch, found by the batch link.
  - `void_submitter_matches` voids every match one submitter recorded on or after a date. The date is the recording time (`created_at`, in UTC), not `played_on`, because the submitter controls `played_on`. An Account's scope does not include its applications' matches.
- **Each match is voided on its own.** Each goes through `void_match` in its own transaction, so it keeps its own `match.void` Audit Event and, for a match that counted, its `match_changed` signal and replay request. A disputed match is voided through `void_match` too, so it records `match.void`, not `dispute.outcome` as the single void action does.
- **Moderators only.** Anyone but an active Account with `matches.moderate_match` gets `MatchActionDenied` before anything is read. A reason is required, and a batch or submitter that is not a stored row is refused before any match is read. Because each match goes through `void_match`, a moderator whose own Player is in a match's correction chain is refused on that match (ADR 0029).
- **What a run reports.** A run returns three counts: voided, skipped (already voided), and refused (the moderator may not void it). After a run that finished, running it again voids nothing and reports the voided matches as skipped. A run that stopped partway is resumed by running it again.
- **A record of the run.** Each run records one `match.bulk_void` Audit Event on the batch or the submitter, with the scope, the date, the three counts, and the reason. It is recorded for every run that finishes, even one that voids nothing.
- **Two admin actions.** The moderator match admin gets "void every match in the selected match's upload batch" and "void every match its submitter recorded on or after the selected match's day (UTC)". The moderator selects one match and types a reason in the action bar. A confirmation page then shows the scope and how many matches are not voided and already voided, and nothing changes until the moderator confirms. ADR 0029 rejected confirmation pages for the single-match actions. These actions are different, because they reach matches the moderator did not select.

## Consequences

- A whole upload can be undone in one action, and a replay then gives the ratings from before it.
- **Gap: a correction leaves the batch.** A correction made by `edit_match` does not join its match's batch, as #205 asks. The batch void finds the corrected match already voided and skips it, while the correction still counts. Moderators, the Event's verified Organizer settling a dispute, and players correcting a self-reported match (ADR 0034) call `edit_match`. A batch match always has an Event, so only the first two can correct one. The submitter scope does reach such corrections, since a correction keeps the original submitter. A player's correction of a self-reported match (ADR 0034) is the exception: its submitter is the correcting player, so the submitter scope finds it under that player. If editing opens to other actors, the batch void may need to follow `corrects` forward.
- **Gap: a confirmed duplicate stays confirmed.** A batch match that exactly duplicates a pending match confirms that match instead of creating one (ADR 0013). The pending match keeps its own submitter and its own batch link, if any, so the batch void does not reach it, and when another submitter recorded it, neither does the submitter void. Only its `match.confirm` Audit Event names the batch's submitter. #71 and #72 decide whether a batch path may confirm this way. Closed by ADR 0036: a batch submission never confirms a pending match.
- **A run reads its list of matches once.** Matches recorded while it runs are left for the next run, and the page's count can differ from the result.
- **The admin runs the whole loop in one web request.** A very large scope can pass the server's request timeout. The voids done so far stay, but that run records no `match.bulk_void` event. Running it again finishes the rest.
- An Account, Partner Application, Event, or Club that a batch names cannot be hard-deleted, even when the batch is empty. Account deletion (#116) must plan for this.

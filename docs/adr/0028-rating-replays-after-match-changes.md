# 0028. Rating replays after match changes

- Status: Proposed
- Date: 2026-11-24

## Context

Issue #47 recalculates ratings when what counts changes: a Match that counted is voided, corrected, disputed, or held by a new possible duplicate flag, a held Match is released (`match_changed`), or a live update cannot apply a Match (a late match, or a snapshot that cannot continue the players' ratings, ADR 0027). Authoritative ratings come from a deterministic replay of the Ledger (hard rule 3), every replay's provenance is stored (hard rule 4), and the backend never computes a rating (hard rule 2). Rating changes are a security-sensitive area (hard rule 8).

The engine's `replay()` can resume only from one checkpoint dated on or before every changed row. Edits and voids are almost always of matches played before the last replay, and a replay of only the affected players' matches could differ from a full replay, since the engine's connectivity looks at the whole pool. A void can also leave a player with no counted match in a format, and the engine then has no rating for them at all.

The project owner settled these points in a comment on #47 (2026-11-24).

This amends ADR 0025 (a replay removes a rating, and orders its changes against live updates) and ADR 0027 (a second snapshot writer, and new live update checks).

## Decision

- **Always a full replay.** `apps.ratings.services.replay_ratings()` replays every Match in `Match.objects.counted()` (through `engine_adapter.run_replay()`) as of `replay_as_of()`: today's UTC date, or the latest counted `played_on` when that is later, since the engine skips a match dated after `as_of`. It never resumes from a checkpoint, so a row whose content changed without a status change (a late confirmer, ADR 0024) cannot raise the engine's `LedgerConflictError`. The earliest affected date is kept for the log only.
- **Requests, then one debounced task.** A change stores a `ratings.RatingReplayRequest` row (its earliest date and a reason: a `MatchChange` value, `late`, or `replay_needed`; no Player, no personal data) in the change's own transaction, through `request_replay()`: a receiver on `match_changed`, and `schedule_replay()` from the live update. After commit it queues `apps.ratings.tasks.replay_ratings` on the `ratings` queue with no arguments and a countdown of `RATING_REPLAY_DELAY_SECONDS` (60). The receiver only inserts that row: the workflow holds Match locks there, and the live update takes the snapshot row before a Match, so taking the snapshot row there could deadlock. A rolled back change leaves no request.
- **Coalesced and idempotent.** The replay runs only while a request is pending. It checks for one without a lock, then locks the `EngineSnapshot` row for the loaded config, reads the requests again, reads the Ledger, and at the end deletes exactly the requests it read. So quick successive changes share one replay, a redelivered or repeated task writes nothing, and a change committed during a replay keeps its own request and task.
- **One transaction, one lock order.** The replay locks the snapshot row first, as the live update does, so replays and live updates serialize. Then it locks every Player it rated or that has a stored rating, in one query ordered by row id, then each state through `apply_rating()`. It never locks a Match row. It stores a `RatingReplay` (provenance), applies every replayed rating with kind `replay`, removes every stored rating the replay no longer produces, and saves the replayed engine's snapshot on the row, in one transaction. A failure rolls everything back and leaves the requests pending.
- **A second snapshot writer.** The replay saves its snapshot on the `EngineSnapshot` row through the same `_save_engine_snapshot()` as the live update. It takes the snapshot right after `replay()`, before any other engine call. Live updates continue from it. Since the replay writes states and the snapshot together, the live update's empty-snapshot check stays valid. The live update's own calls to `schedule_replay()` store reason `late` for every case; the reason is for the log only.
- **Removing a rating.** When a replay produces no rating for a stored state (a void or edit removed the player's last counted match in that format), the replay deletes the `RatingState` through the private `_remove_rating()`, the only path that deletes one. It appends a `RatingHistory` row of the new kind `removed`, which names the replay, no Match, and no delta, and keeps the removed values, and records one `rating.change` Audit Event with the old values as `before` and no `after`, before the delete. `apply_rating()` refuses kind `removed`, and the ORM's own delete paths keep refusing.
- **The live update checks the snapshot first.** Under its locks, the live update returns `unchanged` when the stored snapshot already holds the Match (`engine_adapter.snapshot_holds_match()`), before its history, correction, and late checks, so a Match a replay already counted asks for no further replay. A Match with a live update history row that the snapshot no longer holds (a replay removed it while it was disputed, and a moderator then confirmed it) goes to `schedule_replay()` and `replay_needed`, since `apply_rating()` allows one live update row per Match.
- **Lost work goes to a replay.** A broker error when queuing a live update, and a live update task that fails in a worker, store a request (reason `replay_needed`) in their own transaction, so the next replay rates the Match. A live update task stopped by the hard time limit, or lost with its worker, runs no handler and stores none: its Match waits for the next replay that some other request starts.
- **A sweep.** A beat entry runs the replay task every 15 minutes. It does nothing when no request is pending, and runs the requests whose queued task was lost.
- **Failures are logged without values.** A replay error is logged with its class only, then re-raised. In a worker the task replaces it with a fixed-text `ReplayFailed`; in eager mode the original propagates. The task is not retried: the requests stay pending for the next request or the sweep.

## Consequences

- Every replay rebuilds every rating. The first replay of each UTC day moves `calculated_as_of` for every rated player, so it writes one history row and one Audit Event per rated state (ADR 0025 accepted this). Later replays that day write rows only for ratings that changed.
- A replay holds the snapshot row and every rated Player's row (`FOR NO KEY UPDATE`) until it commits. Live updates wait for it, and so does any update of a Player row. A live update that times out while waiting stores a request, so the next replay rates its Match.
- A Match dated later than today's UTC date (allowed up to UTC+14) moves the replay's `as_of`, and so every player's `calculated_as_of`, to that date.
- A replay that fails the same way every time (one Match the adapter cannot convert, say) leaves its requests pending, and each new request and each sweep runs it again. The log holds only the error class.
- A hold from a near duplicate, or a dispute, now removes a counted Match's effect from the stored ratings at the next replay, until moderator review (#40) resolves it (ADR 0011, ADR 0013).
- Nothing schedules a replay without a request: no daily replay runs yet.

## Alternatives considered

- Resume from a stored checkpoint when the change is after it. Rejected: edits and voids almost always predate the last replay, and a second stored snapshot adds a second state to keep consistent.
- Replay only the affected players' matches. Rejected: results could differ from a full replay.
- Mark a removed rating instead of deleting it. Rejected by the project owner: every reader would have to filter it, and a full recompute has no row for that player.
- Store the request on the snapshot row. Rejected: the workflow's transaction would lock that row while holding Match locks, against the live update's order.
- Debounce through the cache. Rejected: clearing the cache drops the marker (and the queued tasks, ADR 0006), and a request row rolls back with its change.

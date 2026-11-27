# 0058. The full recompute command

- Status: Proposed
- Date: 2026-11-27

This builds on [ADR 0028](0028-rating-replays-after-match-changes.md) and does not change it.

## Context

Issue #48 asks for `manage.py recompute_ratings`: after an engine upgrade, a new parameter set, or a new rating-spec tag, every rating must be rebuilt from the whole Ledger (hard rule 3), and backtests need the same replay without touching stored ratings. The regular replay (ADR 0028) already rebuilds every rating, but it runs only while a replay request is pending, always as of the replay date, and always under `OPEN_RATING_SPEC_VERSION`.

The issue asked for writes "in batches inside transactions". ADR 0028 writes the ratings and the engine snapshot together, in one transaction that holds the snapshot row lock, so a live update never continues from a snapshot that disagrees with the stored ratings. The project owner settled these points in a comment on #48 (2026-11-27).

## Decision

- **The same replay, without a request.** `apps.ratings.services.recompute_ratings()` replays every Match in `Match.objects.counted()` through `engine_adapter.run_replay()`, whether or not a request is pending. A write shares its body with `replay_ratings()`: one transaction, the snapshot row locked first, the `RatingReplay` stored, every rating applied through `apply_rating()` or removed through `_remove_rating()`, and the snapshot saved. There are no batches.
- **Requests.** A write deletes the replay requests that existed when it took the snapshot lock, since it covers them. A request stored after that stays, with its own task.
- **Dry run.** `--dry-run` replays and compares the result with the stored ratings, the way `apply_rating()` decides whether anything changed, and prints counts of created, changed, removed, and unchanged ratings (and, at verbosity 2, each change by OPEN Code and rating). It takes no lock and writes nothing, not even a snapshot row.
- **Options only a dry run takes.** `--format` narrows the output to one rating format, since the engine replays every format together. An `--as-of` before the latest counted `played_on` would drop later matches from the stored ratings. A `--spec-version` other than `OPEN_RATING_SPEC_VERSION` would be put back by the next regular replay. A write refuses all three with fixed text (`RecomputeRefused`) before it writes anything. So to change the spec version, bump the setting, deploy, then run the recompute.
- **Safe to run again.** A second run writes a new `RatingReplay` (its provenance), but `apply_rating()` writes no history row for a rating whose values did not change.
- **Failures.** Like the replay, an error is logged by class only and rolls the write back. The command prints only the error class.

## Consequences

- A recompute holds the snapshot row and every rated Player's row until it commits, like the regular replay. Live updates wait for it.
- The recompute's ratings equal the incremental ones in rating, matches played, last played, and established. RD and reliability depend on the date a summary is taken as of: they are equal for players who played on the replay date, and for every other player the engine's inactivity rule makes the replay's RD at least the live one. A test checks this.
- A later `--as-of` than today is allowed and moves every player's `calculated_as_of` to that date until the next regular replay.

## Alternatives considered

- Commit in batches. Rejected by the project owner: the stored ratings and the snapshot could disagree between batches (ADR 0028).
- Write one format's ratings. Rejected: the snapshot holds every format, so the other formats would disagree with it.

# 0025. Rating state, rating history, and replay provenance

- Status: Proposed
- Date: 2026-11-24

## Context

Issue #45 adds the tables that hold ratings. The engine computes every rating (hard rule 2); the backend must store what it returns, keep an append-only record of each change (hard rule 3), store each replay's provenance so any published rating can be reproduced (hard rule 4), and keep imported reliability apart from OPEN-earned reliability (hard rule 5). Rating changes are a security-sensitive area (hard rule 8).

The issue's words differ from the engine's in places. The issue says "uncertainty" and "last match date"; the engine says `rd` and `last_played`. The issue says "provisional", which has two readings in the project: a rating that holds live updates not yet replayed (hard rule 3, CONTEXT.md Live Update), and a rating that is not yet established (#122).

The rating-spec repository has published no version yet, but each change must name the spec version it follows (decision Q7 in #1).

## Decision

- **Three models** in `apps/ratings`:
  - `RatingState`: a Player's current Rating in one rating format, one row per Player and format (a unique constraint).
  - `RatingHistory`: one row per change, holding the values after the change and its cause. `kind` is `live_update` (it names its `match`, and the engine's `delta` for the player) or `replay` (it names its `replay`). Check constraints tie the kind to its links.
  - `RatingReplay`: every `ReplayInfo` field, the installed engine's version, the spec version, and when it was stored.
- **Engine names win.** The stored fields are `engine_adapter.RatingSummary`'s, converted from `open_engine.PlayerSummary` in the adapter: `rating`, `rd`, `reliability`, `imported_reliability`, `established`, `matches_played`, `last_played`, `calculated_as_of`, `live_updates_since_recalculation`, `config_version`, `params_version`, and `parameter_fingerprint`. Both readings of "provisional" are stored: `established` as the engine gives it, and `provisional`, a property that is True when `calculated_as_of` is empty or `live_updates_since_recalculation` is above 0. So #53 can show either without a migration. The seed provenance fields (#122), `account_linked`, and `connectivity` are not stored.
- **Values as the engine gives them.** Floats, never rounded or computed by the backend. The check for "nothing changed" compares exactly: the engine is deterministic, and a tolerance would be rating math.
- **Versions on every row.** Each state and history row stores `engine_version` and `spec_version`. A live update stores the installed engine's version and `settings.OPEN_RATING_SPEC_VERSION`. A replay change stores its replay's, so a row never disagrees with its replay. `OPEN_RATING_SPEC_VERSION` is a constant in settings, `"unpublished"` until the governing board publishes a spec version; changing it is followed by the recompute (#48).
- **One writer.** `apps.ratings.services.apply_rating()` changes a state and appends its history row, and `record_replay()` stores a replay. The ORM refuses to save an existing history or replay row, to delete one, and the bulk query paths and upserts, as the match ledger guard does. A state is saved only by `apply_rating()` and never deleted or changed in bulk.
- **Audit.** Every change records one `rating.change` Audit Event in the same transaction, with no actor, targeting the state: the old values (none for a new state), the new values, the kind, the Match's OPEN Code, and the replay's id. An apply with the values already stored writes nothing, so a repeated task changes nothing. `record_replay()` records no event: a replay itself changes no rating.
- **Refusals.** `apply_rating()` refuses, with fixed text and before any write, a summary for another player, an unknown format, a cause that breaks the kind rules, an unsaved record, a Match the player did not play in, and a live update in a format the Match does not count in. A doubles Match counts in its own format and in overall doubles, since the engine feeds a category match into overall doubles.
- **Links use `PROTECT`.** A Player with a Rating, a Match that caused a rating change, and a replay that produced one cannot be deleted.

## Consequences

- A full replay records one Audit Event per changed state. The issue asks for an event on every rating change, so this is accepted.
- `RatingState` and `RatingHistory` are personal data (linked to a Player), listed in the personal data inventory. A category format reveals a gender division, as ADR 0009 accepts for ratings. `RatingReplay` names no Player.
- Account deletion (#116) must plan for the `PROTECT` links, as it already does for Matches.
- The callers own transactions and ordering: the live update task (#46) and the replay task (#47). `apply_rating()` locks the state row until the outermost transaction ends. Two first applies for the same player and format at once: one fails on the unique constraint.
- Every stored rating is a demonstration rating while the engine is a demonstration package. #53 labels it in responses.
- Raw SQL and `loaddata` are not guarded, as for the match ledger.

## Alternatives considered

- Use the issue's names (`uncertainty`, `last_match_date`). Rejected: CONTEXT.md and the engine name these `rd` and `last_played`, and two names for one value invite mistakes.
- Store only one meaning of "provisional". Rejected: the project uses both, and a later migration would be needed for the other.
- Read the spec version from the environment. Rejected: like `OPEN_ENGINE_PARAMETER_SET`, a change needs review and a recompute, so it is a code change.
- Store a history row even when nothing changed. Rejected: a retried task would grow the history and the audit log with no change.

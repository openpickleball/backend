# 0011. Duplicate match detection

- Status: Proposed
- Amended by: [ADR 0034](0034-lowest-level-resolution.md) (a possible duplicate hold ends when each match is vouched for at its own level, or after a fixed period that voids the newer match; moderators no longer resolve the flag)
- Amended by: [ADR 0021](0021-organizers-are-the-trusted-submitters.md) (only a submitter with Event authority confirms a pending exact duplicate; club authority is gone)
- Amended by: [ADR 0036](0036-csv-results-files.md) (a batch submission never confirms a pending exact duplicate; the all-or-nothing upload mode is deferred)
- Date: 2026-11-15

## Context

The same match can reach OPEN more than once: a player, an organizer, and a platform may each submit it (issue #25). Counting it twice would move ratings twice. Duplicate detection is a security-sensitive area (CLAUDE.md, hard rule 8), so a bypass is a security bug.

Several paths will submit matches: the first-party endpoints (#31), the partner endpoints (#32), CSV upload (#71), and bulk partner submission (#72). Two of them can submit the same match at the same moment, so a check that reads before it writes is not enough on its own. A partner that resends its own match identifier must get a 409 with the existing OPEN match code (decision F2 in #1).

## Decision

- Every submission path creates a Match through `apps.matches.services.record_match()`. It runs the score rules (`validate_match()`) and every model rule except the unique constraints before it checks for duplicates, so invalid input raises `MatchValidationError`, never `DuplicateMatchError`. `config/api_errors.py` maps `MatchValidationError` to a 422 for every endpoint (ADR 0015).
- A fingerprint identifies a match: the SHA-256 of both teams (each a sorted set of players, the teams in a fixed order, with each game's scores swapped to match), the date played, and the game scores. Team order and player order do not change it.
- An exact duplicate (the same fingerprint as a Match that is not voided, from any submitter) is refused with `DuplicateMatchError`, except that a submitter with Event or Club authority confirms a pending one (ADR 0013), outside a batch submission (ADR 0036). A partial unique constraint on the fingerprint, for Matches whose fingerprint is not blank and that are not voided, backs this in the database.
- A repeated `external_id` from the same Partner Application is refused whatever the earlier Match's status. `record_match()` raises `DuplicateMatchError` with the code, even when the database constraint is what catches it.
- A near duplicate (the same two teams on the same day with other scores) is saved, with one `MatchFlag` per earlier Match. A flag holds both Matches from counting until moderator review resolves it, whatever the new Match's status or submitter (#40 adds review; #26 and #44 must apply the hold). ADR 0013 applies the hold and ends it early when either Match is voided. Flags are their own rows, so review never edits a Match in the ledger. The same players split into other teams is not a near duplicate: partners rotate in recreational play.
- Postgres advisory locks, one per external id and one per lineup and date, make concurrent submissions of the same match wait for each other. The fingerprint constraint catches an exact duplicate that skipped the locks, but only when the Match has a fingerprint. The external id constraint catches a repeated external id. Nothing in the database backs near-duplicate flagging. The locks last until the outermost transaction ends, so each call must run in its own transaction.
- A `DuplicateMatchError` returns 409 with an `ErrorResponse` and one `errors` item (field `match` or `external_id`). This adds to the errors ADR 0005 lists, and is an exception to its rule that `errors` is empty except for validation errors. #31 and #32 declare the 409 response on their operations. Its top-level `code` is `duplicate_match` for both kinds; the item's `code` says which. The message names the earlier Match's OPEN Code only to a caller who may know it: the same Account or Partner Application submitted it, or the submitting Account's Player played in it. Anyone else gets the same 409 without the code. When the message names the code, the `errors` item also carries it as `existing_code` (#199), so a client never parses the message; without the code, the item has no `existing_code` key.

## Consequences

- The same teams playing the same scores twice on one day count as one match. No issue owns recording a real repeat yet.
- A 409 still tells any submitter that a match with those players, date, and scores exists, even without the code. This holds for a player whose match history is private, and ADR 0009 leaves players aged 13 to 17 out of all partner sharing, so #31 and #32 must decide how their 409 treats such a match. #31 decided it in ADR 0015: a self-submitter is always on the match it duplicates. A wrong guess is saved: as a near duplicate when the same teams played that day, otherwise as a new Match.
- Any submitter can hold a real Match from counting until review, even with a pending submission that is never confirmed, by sending its teams and date with other scores. Holding only the new Match would instead leave a false early submission counted. #79 (who may submit at all) and #40 (review) carry this risk. #79 limits it to permitted submitters but does not tie a near duplicate to the submitter's own Club or Event, so a permitted submitter at one Club can still hold another Club's or Event's Match (ADR 0012). #26 or #44 may refine the hold, for example so a flag from an unconfirmed Match cannot hold a confirmed one. #26 refined it only so that a voided Match no longer holds the other (ADR 0013); a pending Match's flag still holds a confirmed one until it is voided, expires, or is reviewed.
- A permitted submitter's exact duplicate of a pending Match confirms that Match instead of being refused (ADR 0013), except in a batch submission, which is refused like any other exact duplicate (ADR 0036).
- A Match written outside `record_match()` has no fingerprint and escapes the exact check, though a later submission flags it as a near duplicate. The factories set the fingerprint.
- A batch path must not wrap several submissions in one transaction: two such batches can deadlock. It must record each match separately and report a result per match. Nothing enforces this. An all-or-nothing upload mode must get its all-or-nothing result another way. A check of every row before recording any is not enough: a concurrent submission can still make a later row a duplicate after earlier rows have committed. #71 shipped without the mode (ADR 0036).
- The fingerprint uses Player row ids, so merging two Players (identity linking) does not update the fingerprints of their earlier matches.

## Alternatives considered

- Checking duplicates without a lock or constraint: two simultaneous submissions both pass the check and both count.
- Rejecting near duplicates: a wrong early submission would block the real result. Flagging lets a moderator decide.
- A flag field on Match: dismissing a flag would edit a ledger row in place, which hard rule 3 forbids.
- Always naming the earlier Match's code in the 409: any caller could learn a match's code and score by guessing, even for a player whose match history is private or who is 13 to 17.
- Flagging the same set of players in any team split: rotating partners on one day would flood moderators with false flags.

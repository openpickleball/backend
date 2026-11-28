# 0049. Flag unusual submissions from trusted submitters

- Status: Proposed
- Date: 2026-11-26
- Amended by: [ADR 0064](0064-organizer-and-application-vetting.md) (every new Organizer has a trial that holds its matches; a flag still holds nothing)

## Context

A match with an Event comes from a trusted submitter: a member of the Event's verified Organizer, or an approved Partner Application that acts for it (ADR 0021). Such a match is confirmed and counts as soon as it is recorded. A compromised or careless submitter can record many wrong matches at once. Issue #206 asks OPEN to flag unusual submissions automatically, without any automatic penalty: false positives are certain, and a penalty would need an appeals process OPEN does not have. Recovery from real fraud stays with the player notice (ADR 0041), a participant's dispute settled by the Event's Organizer (ADR 0034), a player's report of the Organizer (ADR 0037), and a moderator's unverify and bulk void (ADR 0032).

This amends ADR 0032: a Submission Batch stays insert-only except its `expected_size`, set once while empty.

## Decision

- A new flag reason, `unusual_submission`, is a signal only. It is in `SIGNAL_FLAG_REASONS`, so it never holds a Match from counting (`Match.objects.counted()` reads only open `possible_duplicate` flags), never changes a Rating, and sends no `match_changed`. Nothing refuses, holds, or limits the submitter. No vouch covers it and no one resolves it: moderators resolve no flag (#273).
- `MatchFlag.rule` records which rule fired: `first_matches`, `large_batch`, or `daily_volume`. Each rule that fires gives one flag, so a Match carries at most one flag per rule (a partial unique constraint). An `unusual_submission` flag always has a rule and never a `duplicate_of`; a `possible_duplicate` flag has no rule. #207 adds its own rule to the same field.
- `record_match()` checks the rules only for a new Match with an Event (a trusted submitter's), as its last step inside its own transaction, after the permission checks, so a refused submitter learns nothing and nothing is read before the permission check. A player's self-submitted Match is never flagged by these rules: it already needs both teams' confirmation. A correction made by `edit_match()` and a submission that confirms an existing pending Match are not new submissions and are not flagged.
- The rules, with thresholds from settings (whole numbers from 1 to 1,000,000; a bad value stops startup and names the variable, never the value):
  - `first_matches` (`UNUSUAL_SUBMISSION_FIRST_MATCHES`, default 25): the Match is among the first N recorded on the Organizer's Events since its current `verified_at`, so a re-verification starts the count again. An application submitter also has its own first N since its `approved_at`; either count gives the one flag.
  - `large_batch` (`UNUSUAL_SUBMISSION_BATCH_SIZE`, default 100): the Match came in a Submission Batch whose `expected_size` is over the number. Every Match of such a batch is flagged, the first included. A CSV upload sets `expected_size` to the file's number of match rows (a row that fails included, as the 1000-row limit counts them) once it has parsed the file and before it records any row. It is the one value of a batch that may be written after the insert, once, while it is empty.
  - `daily_volume` (`UNUSUAL_SUBMISSION_DAILY_MATCHES`, default 200): the submitter (an Account or an application) has recorded more than the number of Matches on the Match's UTC `created_at` day, counting voided Matches too, so voiding and sending again does not reset it. The Matches past the number are flagged; all are recorded and count.
  - Club Role holders are not a rule: since ADR 0021 a Club Role gives no right to submit.
- Every count takes only new trusted submissions: Matches with an Event that correct nothing, in any status (voided ones too). A correction made by `edit_match()` keeps its original's submitter and Event, so counting it would let an Organizer use up its first N by editing one match, or push another member over the daily number. A player's self-submission is not a trusted submission and does not count toward the daily number either. The flag a corrected Match carried stays on it, voided, where the Organizer review still counts it.
- The counts are bounded by a `LIMIT` of one past the threshold. A transaction-level advisory lock per submitter, taken after every other lock in `record_match()`, makes one submitter's counts exact under concurrent submissions. After it the call takes no new lock (commit-time foreign key checks share-lock only rows the transaction already holds, or rows that no transaction waiting for this lock holds `FOR UPDATE`: before it, `record_match()` takes only share locks and advisory locks, and a match row it locks to confirm a pending duplicate is covered by the lineup lock), so it adds no lock cycle. Two members of one Organizer submitting at once can both be counted inside the first N; that errs toward a flag.
- Who sees the flags: the Organizer, in its flag feed (#213, not built yet). Moderators see them only as counts in the Organizer's review (`organizer_review()`, ADR 0037), to decide on its verification. The match admin does not list them, and no API response or email shows them.
- Nothing reads payment status (hard rule 7).

## Consequences

- A newly verified Organizer's first matches are flagged, so a moderator reviewing a report sees a count that is high for a new Organizer by design. The review's other counts (disputes) say more.
- A large tournament that uploads more than 100 matches in one file, or records more than 200 in a day, gets flags on real results. The flags change nothing for players, so the cost is noise in the counts, which the settings can tune.
- The first-N window compares a Match's `created_at` with the Organizer's `verified_at` (or the application's `approved_at`), times from different hosts. A verification that commits while a match is being recorded, or clock skew, can leave a match outside the window, which only errs toward a flag.
- The review counts flag rows, so one match can add up to three.
- Concurrent submissions of one submitter wait for each other only for the end of their transactions.
- #213 will show the flags to Organizers; #207 will add its rule to `MatchFlag.rule`.

## Alternatives considered

- Holding flagged matches until review: rejected by the issue. A hold is a penalty without appeal, and false positives are certain.
- Flagging only the rows past the batch threshold: simpler, since `record_match()` sees one row at a time, but the issue asks for every match of a large batch, so the batch states its size first.
- Counting the first N per submitting Account: an Organizer could add members to reset it, so the count follows the Organizer.
- No lock for the daily count: concurrent submissions would each miss the others, so a burst could stay under the number. The lock is short and per submitter.

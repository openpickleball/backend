# 0013. Match confirmation and changes

- Status: Proposed
- Amended by: [ADR 0034](0034-lowest-level-resolution.md) (a disputer can withdraw a dispute, players correct a self-reported match, with the correcting player as the correction's submitter, the Event's Organizer resolves a dispute on its match, an unsettled dispute voids the match after a fixed period, and moderators no longer resolve, void, or edit single matches)
- Amended by: [ADR 0021](0021-organizers-are-the-trusted-submitters.md) (every confirmed-on-creation match has an Event, and the duplicate path confirms a pending match only for a submitter with Event authority)
- Amended by: [ADR 0036](0036-csv-results-files.md) (a batch submission never confirms a pending exact duplicate)
- Date: 2026-11-16

## Context

A match should move ratings only once both teams agree on it, or once someone trusted vouches for it (issue #26, decisions F1 and F4 in #1). Players must be able to dispute a result, moderators must be able to rule on disputes, and partners and moderators must be able to fix mistakes (#33, #40). The ledger is append-only (CLAUDE.md, hard rule 3), and score confirmation is a security-sensitive area (hard rule 8). Ratings work (#44, #46, #47) and webhooks (#85) need to know when a match starts or stops counting.

## Decision

- A Match has four statuses: pending, confirmed, disputed, and voided. The services in `apps.matches.workflow` make every status change except one: `record_match()`'s duplicate path, below, confirms a pending match. Nothing changes any other field of a Match once it is written.
- `record_match()` creates a match with an Event or a Club confirmed, since its submitter passed `can_create_match` (ADR 0012, decision F4). A self-submission (no Event, no Club) is created pending.
- A pending match becomes confirmed when at least one player of each team has confirmed it (`confirm_match`), or when a member of its Event's verified Organizer, or an approved application that Organizer owns, confirms it. A player's submission through `create_match` counts as their team's confirmation (issue #197): the endpoint calls `confirm_match` for the submitter in the transaction that records the match, which adds the submitter's `MatchConfirmation` row like any other participant's confirmation. The match stays pending until a player on the other team confirms it, a singles match included, so one team alone never confirms a match. `record_match()` itself records no confirmation. Each player's confirm or dispute is a `MatchConfirmation` row, at most one of each per player and match.
- A permitted submitter (Event or Club authority) whose submission exactly duplicates a pending match confirms that match instead of getting a 409. `record_match()` returns `RecordedMatch(match, created=False)`. The pending match keeps its own fields: it gains no Event, Club, or external id. A retry by the same submitter returns the same result, recognized by its earlier `match.confirm` Audit Event. A batch submission (a CSV upload, #71, or a bulk request, #72) never confirms this way: its exact duplicate is refused (ADR 0036).
- A participant may dispute a pending or confirmed match once. It becomes disputed and stops counting until a moderator resolves it as confirmed or voided, with a required reason (`resolve_dispute`, Audit Event `dispute.outcome`). An actor allowed to void it can also end the dispute with `void_match` (`match.void`), and a moderator with `edit_match` (`match.edit`).
- A void is a status change. An edit voids the old match and creates a corrected one, with a new OPEN Code and `corrects` pointing to the old one, through the same validation, model rules, locks, and duplicate checks as `record_match()`. The correction keeps the old match's submitter, Event, Club, and external id. A player's correction of a self-reported match is the exception: its submitter is the correcting player's Account (ADR 0034, maintainer decision on #267). It is pending when the old match was pending, and confirmed otherwise, so a moderator's edit of a disputed match also confirms the correction. A moderator (permission `matches.moderate_match`) may void or edit any match. Anyone else needs `can_change_match`, and an edit also needs `can_create_match` for the stored Event and Club and the new players, so an edit never creates a match its actor could not submit. Only a moderator edits a disputed match, and only a moderator's reason is stored.
- A match counts when it is confirmed and no `possible_duplicate` flag links it to a match that is not voided (`Match.objects.counted()`). This applies ADR 0011's hold and refines it in one way: a voided match no longer holds the other one.
- `match_confirmed` is sent once each time a match becomes confirmed, except a correction of a counting match, which sends `match_changed` with `change=edit` instead. `match_changed` is sent when a counting match is voided, corrected, disputed, or held, and when a held match counts again, with the earliest affected date. Both are sent synchronously inside the transaction, after the rows and the Audit Event, so a receiver can write an outbox row in that transaction and must enqueue other work with `transaction.on_commit`.
- Every status change writes one Audit Event: `match.confirm`, `match.dispute`, `match.void`, `match.expire`, `match.edit`, or `dispute.outcome`. A refused or repeated action writes none.
- With `MATCH_CONFIRMATION_EXPIRY_DAYS` set (1 to 36500), a daily task voids pending matches submitted (`created_at`) more than that many days ago. Unset, the task does nothing.
- Every status change takes the match's lineup advisory lock (the key `record_match()` uses) before its row lock, so whether flag-linked matches count is read and changed one transaction at a time.

## Consequences

- The ORM refuses to delete any Match, Match Participant, Game, or Match Confirmation, and refuses to change any of their fields except a Match's status. Raw SQL, raw saves (`loaddata`), and migrations' historical models are not guarded. Any code can still call `save(update_fields=["status"])`, so the workflow is the rule, not something the model enforces.
- A partner's external id is unique among matches that are not voided, so a correction keeps it. `record_match()` still refuses an external id used by a match in any status (decision F2).
- When a submission confirms a pending match through the duplicate path, its external id is not stored, so the application may later use that id for another match. The confirmed match stays a self-submission for #44's match type.
- Any Club Role, a match manager included, can confirm a player's pending rec match by sending its exact teams, date, and scores, and so learns its OPEN Code. `confirm_match` itself gives no confirm power to club authority. The same actor could have submitted the match as confirmed, so this was accepted.
- A participant's confirmation that does not complete a match writes a `MatchConfirmation` row and no Audit Event (no status change), so the submitter's confirmation at submission has no `match.confirm` event of its own. The event is written, with the other team's confirming Account as actor, when the match becomes confirmed.
- A match submitted through `create_match` is not in the awaiting-confirmation list of the submitter or their teammates, and the response to `create_match` shows `my_decision` `confirm` and their team as confirmed.
- In practice a pending match never has an Event, since `record_match()` creates Event matches confirmed. The duplicate path is how an Organizer confirms a player's pending match.
- `match_confirmed` can fire for a match that a duplicate flag holds. Receivers check `counted()` before rating it.
- A receiver that raises rolls the whole change back. One match that cannot expire is logged by id and skipped, and the run then fails, so the others still expire.
- An active superuser is a moderator.
- Dispute text is not stored in the audit log, which cannot be erased. #31 decided that a dispute takes no text, so none is stored (ADR 0015).
- `MatchFlag` rows are not guarded. #40 adds flag resolution, and must send `match_changed` when it releases a hold.

## Alternatives considered

- Editing a Match in place, with the old values kept in its Audit Event. Rejected: hard rule 3 keeps the ledger append-only, and a replay must see every Match as it was submitted.
- A separate `expired` status. Rejected: an expired match should free its fingerprint so the players can submit it again, which voiding already does, and the `match.expire` Audit Event tells the two apart.
- Leaving a disputed confirmed match counting until a moderator rules. Rejected: a participant's dispute of a wrong result should stop it moving ratings, and decision F4 lets players dispute.
- Keeping the submission separate from the submitter's confirmation, with the app calling confirm right after submit. Rejected in issue #197: a player who submits a match agrees with its score, and the second request can fail on its own (offline, timeout), leaving a match nobody on the submitter's team has confirmed.
- Refusing a permitted submitter's exact duplicate of a pending match with a 409. Rejected: the organizer's submission is the trusted confirmation the pending match waits for (decisions F1 and F4).
- Sending the signals after commit. Rejected: the webhook outbox (#85) must be written in the same transaction as the change, so a rolled back change sends nothing.
- A database trigger guarding the ledger, like the audit log's (ADR 0007). Not done here: the ORM guard covers app code, and a trigger would also have to allow status changes. It can be added later.

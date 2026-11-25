# 0035. A stored match type, and what may change on an Event with matches

- Status: Proposed
- Amends: [ADR 0024](0024-engine-match-type-confirmers-and-rating-format.md) (the match type is stored on each Match, no longer derived from the Event at replay time)
- Date: 2026-11-25

## Context

An Event's `kind` is the match type its matches count as, and its `organizer` decides whose verified status lets a submitter record counted matches on it (ADR 0021). Until now the admin could change both on any Event, and nothing was recorded. ADR 0024 had the engine adapter read the match type from the Event's current kind at each replay. So changing the kind of an Event with counted matches changed how all of those matches weigh, and the same Ledger replayed differently over time (hard rule 3). Changing the organizer of an Event with matches moved those matches under another Organizer, whose members could then change them (`can_change_match`). Both let one party change other players' ratings (hard rule 8).

The project owner decided on 2026-11-25 (comment on issue #163): once an Event has matches, its `kind` can change and its `organizer` cannot, and the match type is locked per match.

## Decision

- **Stored match type.** Each Match stores its match type (`Match.match_type`: `tournament`, `league`, `rec_verified`, or `self_reported`, the engine's `MatchType` values). `record_match()`'s creation core (`draft_match()`) sets it once: the Event's kind, read from the database by id, for a match with an Event; `rec_verified` for a match with a Club and no Event; `self_reported` for neither. That core also creates `edit_match()`'s corrections, so a correction takes the Event's kind at the time of the correction. The ledger guard keeps the value unchanged afterward. A check constraint ties it to the match's Event and Club: a match with an Event has one of the three Event kinds, a Club match with no Event is `rec_verified`, and a match with neither is `self_reported`. The database cannot compare it with the Event's kind, since that may change later by design.
- **The adapter reads the stored value**, never the Event. This replaces the match type rule in ADR 0024; its other rules stand.
- **Kind may change.** Changing an Event's kind changes only matches recorded after the change.
- **Organizer is locked once an Event has a match**, in any status, voided included. `Event.save()` refuses the change (`Event.clean()` reports the same message for the admin form). To serialize with a submission in flight, `Event.save()` locks an existing Event row `FOR UPDATE` on every save and compares the stored organizer under that lock, and `record_match()` and `edit_match()` take the Event row `FOR KEY SHARE` at the start of their write transaction and check the submitter's permission again. Either the organizer change waits for the match to commit and then refuses, or the match waits for the change to commit and is checked against the new Organizer. The match reads the Event's kind again under the same lock, so a kind change and a match in flight run in turn too: a match committed after a kind change has the new kind. The foreign key alone does not give this order: Django creates it deferred, so it is checked only at commit.
- **Audit.** Every Event create, change, and delete through `apps.events.services` (`save_event()`, `delete_event()`) records one Audit Event (`event.create`, `event.change`, `event.delete`) with the acting Account, in the same transaction. A change records only the changed fields, old and new. The values are the name, kind, the Organizer's and Club's OPEN Codes, and the dates; none is an identifying field (ADR 0009). The admin uses these services, and #125's API will too.
- **Existing matches** got their match type from a data migration that applies the old rule (the Event's kind at migration time, then Club, then neither), so every stored value equals what the adapter derived just before. The migration writes through the historical model, which has no ledger guard.

## Consequences

- A kind change no longer moves past matches' ratings, so a replay of the same Ledger is repeatable.
- The three matches migrations (add the field, fill it, then require it and add the constraint) must run together with no older code writing Matches in between: a Match inserted between the fill and the last step without a match type makes that step fail. Nothing has been deployed yet.
- A correction of an older match takes the Event's current kind, which may differ from the match it corrects. This follows from setting the value in the creation core, as `rating_format` is.
- Events become a security-sensitive area: `/apps/events/` gets its own CODEOWNERS entry.
- The Club of an Event with matches can still change. The permission rules read the Event's stored Club for club consent on new submissions only.
- A plain ORM save or delete of an Event outside the services, and raw writes (`QuerySet.update`, `bulk_create`, raw SQL, `loaddata`), record no Audit Event, and raw writes also skip the organizer lock.

## Alternatives considered

- Lock `kind` too. Not chosen: the project owner decided the kind may change, with past matches keeping the type they were recorded with.
- Change kind only through a correction the replay can reproduce. Rejected: it needs a new ledger record type for Events, and storing the type per match gives the same repeatability.
- Keep deriving the type from the Event and audit kind changes. Rejected: the audit row records the change but does not stop past ratings from moving.

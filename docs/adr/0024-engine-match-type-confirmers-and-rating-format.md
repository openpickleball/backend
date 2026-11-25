# 0024. Engine match type, confirmers, and a stored rating format

- Status: Proposed
- Amended by: [ADR 0033](0033-lowest-level-resolution.md) (moderators no longer confirm self-reported matches, so the every-participant confirmers rule has nothing to apply to)
- Date: 2026-11-23

## Context

The engine adapter (`apps/ratings/engine_adapter.py`, issue #44) turns each counted Match into an engine `Match`. Three of the engine's inputs are not plain copies of a stored field.

- **Match type.** It sets a match's weight and whether the match counts toward "established". The engine requires the backend to set it from organizer, league, or club provenance, never from what a submitter chooses. An Event's `kind` gives it for an Event match. A match with no Event was mapped to self-reported.
- **Confirmers.** The published parameter set `v1` has `self_reported_requires_both_teams` on. So the engine rejects a self-reported match (`self_reported_unconfirmed`) unless `confirmed_by` names a player from each team. The backend can count a match with no Event that lacks a confirmation from each team. A Club match is created confirmed with no Match Confirmations (decision F4). A moderator's correction (`edit_match`) gets no Match Confirmations. A moderator can rule a dispute confirmed when only one team confirmed (`resolve_dispute`). If `confirmed_by` were built only from Match Confirmations, the backend would count these matches and the engine would silently never rate them.
- **Rating format.** The backend stores singles or doubles. For doubles, the engine also has men's, women's, and mixed doubles, which come from the players' gender divisions. A division can be removed later (#190, #116). If the format were read from current divisions at each replay, that removal would move the player's past matches into overall doubles. The other three players' category ratings would change with it, and the same Ledger would replay differently over time (hard rule 3).

The project owner decided all three on 2026-11-23 (comment on issue #44).

## Decision

- **Match type.** A match with an Event has its Event's kind. A match with a Club and no Event is `rec_verified`, because the Club stands behind it. A match with neither is `self_reported` (decision F1). Nothing a submitter sends sets it.
- **Confirmers.** A match with an Event or a Club sends the players with a confirm decision (`MatchConfirmation`), which the engine does not need for those types. A self-reported match that became confirmed through a moderator action sends every participant as `confirmed_by`: the moderator's decision stands in for the teams. Every other self-reported match sends only the players with a confirm decision.
- **What counts as a moderator action.** The adapter reads it from the audit log, never from a guess:
  - a `dispute.outcome` Audit Event on the match with the outcome `confirmed`; or
  - the match is a correction, and the `match.edit` Audit Event on the match it corrects shows that match was confirmed or disputed, so the correction was created confirmed. Only a moderator can edit a match with no Event and no Club (ADR 0012, ADR 0013).
- A moderator cannot confirm a pending self-reported match alone through `confirm_match`. The duplicate path, in which an Event submitter confirms a player's pending match, is not a moderator action (a Club submitter could too before ADR 0021). That match stays self-reported (ADR 0013), so it rates only once both teams have confirmed it.
- **Rating format.** Each Match stores one rating format (`Match.rating_format`): `singles`, `doubles`, `mixed_doubles`, `doubles_men`, or `doubles_women`, the engine's rating format values. `record_match()`'s creation core sets it from the players' gender divisions at that moment. That core also creates `edit_match()`'s corrections, so a correction gets its own format. A doubles match is men's doubles when all four players are men's, women's doubles when all four are women's, and mixed doubles when each team has one men's and one women's player. Any other mix, or a player with no division, is overall doubles. The ledger guard keeps the value unchanged afterward, and a check constraint ties it to the match's `format`. The adapter reads the stored value, never the current divisions.

## Consequences

- Club matches now rate, at the `rec_verified` weight, and count toward "established". ADR 0021 is accepted and refuses new matches with a Club and no Event, so this rule covers only matches already in the Ledger (none, since nothing had been deployed).
- A moderator's ruling or correction moves ratings even though the teams did not both confirm. This is a trust placed in moderators, a security-sensitive area (hard rule 8). The `dispute.outcome` and `match.edit` Audit Events record each such action and its reason.
- The adapter depends on those audit rows. Audit rows are append-only (ADR 0007), so the record cannot be changed after the fact. A match whose rows were written outside the workflow (raw SQL, `loaddata`) gets only its Match Confirmations.
- A self-reported match confirmed through the duplicate path with one team's confirmation counts in the backend (`counted()`) but not in the engine, until the other team confirms. A partner or the web app that reads "confirmed" as "rated" must allow for this.
- A stored category format reveals the gender division of all four players. It is listed in the personal data inventory, and the API does not return it.
- Removing a gender division does not change any stored rating format, so past matches keep their category. Account deletion (#116) decides what happens to the stored formats of a deleted player's matches.
- The field's default is `singles`, the only value the check constraint allows for a singles match. A doubles match must set its own value, or the constraint refuses it. Nothing has been deployed. A local database that already holds a doubles match fails the migration when it adds the constraint. Reset it (`docker compose down -v`) and migrate again.

## Alternatives considered

- Read the rating format from current divisions at each replay. Rejected: a profile change would move other players' ratings, and a replay would not be repeatable.
- Copy confirmations onto a correction. Rejected: the teams confirmed the old score, not the corrected one.
- Turn off `self_reported_requires_both_teams`. Rejected: that is an engine parameter, set by the published parameter set, not by the backend (hard rule 2).
- Keep Club matches self-reported. Rejected: they have no Match Confirmations, so the engine would never rate them.

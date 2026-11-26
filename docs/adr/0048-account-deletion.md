# 0048. Account deletion: what goes, what stays, and how a Player is shown

- Status: Proposed
- Date: 2026-11-26

## Context

ADR 0009's Retention section says deleting an Account removes all of its personal data at once, except the Player with its OPEN Code, Matches, and Ratings, the Audit Events, and a cleared Account row for an Account that has acted. It left the mechanics to #116, and so did ADR 0002 (how a deleted player's Player is shown, and whether new Matches can still name it), the personal data inventory (the Organizer that is one person, the agreement rows, the submitter and voucher links), and ADR 0023 (a player who entered their birth year and month or gender division wrong deletes the Account and signs up again).

Many links to an Account use `PROTECT`: every Audit Event actor (ADR 0007, so an Account that changed a password, ended a session, used MFA, reported an Organizer, or acted in the admin), the Player (`Player.account`), Match and Submission Batch submitters, vouches, Organizer verification and application approval. A Player in the Ledger is protected by its Match Participants, confirmations, and ratings.

The maintainer decided five questions on #116:

1. An Organizer whose only member is the deleted Account is that person: its name is replaced with a fixed placeholder, with an Audit Event.
2. No new Match names a Player whose Account is inactive, on any path, the first-party create included.
3. `AgreementAcceptance` rows are deleted.
4. An Account that owns a Partner Application is kept, cleared, so the application (which belongs to an Organizer) survives.
5. Staff and superusers cannot delete themselves through the API: 409.

## Decision

### The operation

- `DELETE /api/v1/accounts/me` (`delete_current_account`) deletes the signed-in Account's personal data and answers 204. It needs a reauthentication within allauth's `ACCOUNT_REAUTHENTICATION_TIMEOUT` (`auth/reauthenticate` with the password, or `auth/2fa/reauthenticate` with a code), read from the session behind the request's authentication, so it works for both headless clients. A login alone does not count. Without one it answers 403 `reauthentication_required`. A staff or superuser Account gets 409.
- The work is `apps.accounts.deletion.delete_account()`, in one transaction.

### What goes

- Every session of the Account (each `UserSession` row and the Django session it names, the app token's session included), and the request's own session. A session made before user sessions were installed has no row and cannot be found without reading every session; the Account is inactive with an unusable password (or gone), so Django refuses it on its next use, and the session sweep (ADR 0047) deletes it when it expires.
- allauth's `EmailAddress` rows, MFA authenticators, `AgreementAcceptance` rows, OAuth tokens and grants issued to the Account (Log in with OPEN grants), the Organizer Reports it filed (an open one is first closed with an `organizer_report.close` event, rule `reporter_deleted`), queued emails to any of its addresses, and Django admin log entries tied to it: its own, and every entry for its Account, Player, privacy row, address, authenticator, session, agreement, membership, role, or report row.
- Organizer memberships and Club Roles, through `apps.audit.access`, so each removal records an Audit Event. An Organizer whose only member is the Account gets the name "Removed organizer" first, with an `organizer.name_remove` event that names the field only. A shared Organizer keeps its name: it is not that person.
- The Player's personal data. A Player nothing protects (no Match, confirmation, or rating) is deleted with its privacy row. Any other Player stays with its OPEN Code: its display name becomes "Deleted player", its legal name, gender division, and city and region are emptied, its birth year and month become NULL (the encrypted fields store NULL, ADR 0046), and every privacy setting is reset to private. The birth year and month columns allow NULL for this only; every new Player still needs them.
- The Account row, when nothing protects it and it owns no Partner Application. Its cascades are recorded first (`record_cascaded_removals`).

### What stays

- A protected Account row, deactivated, with a placeholder email (`deleted-<random hex>@deleted.invalid`, RFC 2606's reserved domain, so no mail goes to it and no one can verify it), an unusable password, no staff or superuser flag, no groups or permissions, and no last login. `date_joined` stays. The links that point to it stay as they are: Audit Event actors, the kept Player, Match and Submission Batch submitters, vouches, `Organizer.verified_by`, `PartnerApplication.approved_by` and `user`.
- The Ledger, unchanged (hard rule 3): Matches with their stored match type and rating format, participants, confirmations, vouches, flags, Submission Batches, and Ratings with their history and the engine snapshot, which names players by OPEN Code. A replay gives every player the same ratings as before the deletion.
- Audit Events (ADR 0007). None holds an identifying value.

### Audit

- One `account.delete` event per deletion, targeting the Account, with `after` saying whether the Account and the Player were kept. Every event the deletion records has no actor when the actor is the deleted Account, since an event naming a hard-deleted Account fails the delete at commit. A staff actor (a later admin path) is recorded as the actor.

### How the Player is shown, and naming it later

- A kept Player's Account is inactive, so the existing rules already hide it: its profile, ratings, and search answer as for an unknown player, and a match lists it as a private player with no code (ADR 0015, ADR 0031).
- No new Match names it. The first-party create, the CSV upload, and `record_match()` itself treat a Player whose Account is inactive as unknown (`unknown_player`). A correction may keep the players of the Match it corrects, so a match with a deleted player can still be corrected, but cannot add another inactive player. This settles ADR 0002's open question for OPEN Codes; #82's mappings are deleted at Account deletion when they land (ADR 0009).

### Signing up again

The placeholder frees the address, so the same email can sign up again and create a new Player, also when the old Player stays in the Ledger. This is how a player fixes a birth year and month or a gender division entered wrong (ADR 0023): delete the Account, then sign up again with the right values. The old matches and ratings stay with the old, deleted Player; the new Player starts fresh.

### Retention task

The scheduled retention task is the session sweep from ADR 0047 (`sweep_sessions`, every 15 minutes), which deletes expired sessions and the user session rows of sessions that ended. ADR 0002 leaves no unclaimed records to age out and ADR 0009 has no inactivity deletion, so it is the only one.

### Outside the live database

- allauth's rate limit cache keys hold the client IP and a SHA-256 of an address for at most an hour, and age out on their own. The new device marker (ADR 0047) lives only in the browser or app.
- Backups age deleted personal data out within 30 days (ADR 0009, #67): a backup taken before a deletion still holds the data until it expires, and is never restored to bring a deleted Account back. #67 sets the retention.
- Copies held by the email provider follow #35.

## Consequences

- Most players who used OPEN have acted (a password change, an ended session, MFA, a report, a confirmation that kept their Player), so most deletions keep a cleared Account row. That is expected.
- A correction made after a deletion derives its rating format from the players' current divisions, and the deleted player's is empty, so such a correction counts in overall doubles rather than a gendered format. The deletion itself changes no stored Match.
- Deleting an Organizer's only member leaves the Organizer with no member and the placeholder name. A verified Organizer stays verified until a moderator unverifies it.
- A match recorded in the moment between a deletion's start and its commit could still name the Player. This is accepted.
- Deleting an Account through the Django admin is not covered by this record: the admin's delete cascades without these steps, and staff should not use it for a player. A later issue can route it through `delete_account()`.

## Alternatives considered

- **Always keep the Account row.** Simpler, but ADR 0009 allows deleting an Account nothing protects, and a hard delete leaves less behind.
- **Clear the rating format of the deleted player's matches.** It would change stored Matches (hard rule 3) and other players' ratings.
- **A flag that marks an Organizer as one person.** More accurate, but staff would have to set it for every Organizer; the only-member rule needs nothing new.
- **Refuse deletion while the Account owns a Partner Application.** It would block a player on an Organizer's setup.

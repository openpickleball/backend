# 0034. Match and player issues are settled at the lowest level

- Status: Proposed
- Date: 2026-11-24

## Context

OPEN is run by volunteers with no money. It cannot staff a team that reviews every disputed or flagged match, or that judges players. Today a disputed match stops counting until a moderator resolves it (ADR 0013). A possible duplicate holds both of its matches until a moderator resolves the flag (ADR 0011, ADR 0029). Several planned issues send signal flags and seed checks to moderators (#50, #51, #123, #175, #206, #207, #209). With few moderators these queues grow, and a held or disputed match can wait forever.

The people closest to a match know the most about it: its players, and for a match with an Event, the Organizer that ran it. The project owner decided on 2026-11-24 that OPEN Moderators approve Organizers and handle administration of the app, and that issues with matches and players are resolved at the lowest level possible.

## Decision

This amends ADR 0011 (who ends a duplicate hold), ADR 0013 (who resolves a dispute), ADR 0015 (an Organizer's members read and settle its Events' disputed matches, #269), ADR 0024 (the confirmers of a self-reported match a moderator confirmed), and ADR 0029 (what moderators do in the admin).

- **Levels.** A match or player issue goes to the lowest level that can settle it:
  1. The players in the match.
  2. For a match with an Event, the Event's verified Organizer: a member of it, or the approved application that submitted the match (the actors `can_change_match` allows).
  3. Time. An issue nobody settles ends after a fixed period, by a rule that keeps a contested result out of ratings.

  OPEN Moderators are not a level for matches or players.
- **What moderators do.** Moderators administer the app and the trust it gives. They verify and unverify Organizers (#40, #229), approve Partner Applications (#29), remove an under-13 player's personal data (#190, a legal duty), and void a submitter's matches in bulk once it has lost trust (ADR 0032). They also handle other administration, such as operations (#141) and webhook endpoints (#87). They do not confirm, void, edit, or approve single matches, resolve disputes or flags, or set ratings.
- **Withdrawing a dispute.** The participant who disputed a match, self-reported or with an Event, can withdraw the dispute. The withdrawal is a new `MatchConfirmation` row, never an edit or delete of the dispute (hard rule 3). The match stays disputed while any participant's dispute is open. When the last one is withdrawn, the match returns to confirmed when its confirmation rule holds (one player of each team confirmed it, or it was confirmed on creation), and to pending otherwise. A withdrawn dispute still counts as that player's one dispute.
- **Disputes on a self-reported match.** The players settle it. Besides withdrawal, a participant can correct a pending or disputed self-reported match, keeping the same two teams. The old match is voided, and the correction is pending, with the corrector's confirmation, until the other team confirms it (as `create_match` does, #197). A confirmed self-reported match must be disputed before it can be corrected.
- **Disputes on an Event match.** After withdrawal, the Event's verified Organizer settles it. It resolves the dispute as confirmed or voided, or corrects the match, with a required reason stored in the Audit Event. An Organizer Member whose Player is in the match's correction chain, or in the correction's teams, cannot do this. That is the rule `moderates` applies to moderators today. Another member, or the application, can.
- **Unsettled disputes.** A dispute still open after `MATCH_DISPUTE_EXPIRY_DAYS` (default 30, never empty) voids the match. A contested result never moves ratings by default.
- **Possible duplicates.** A hold ends when either match is voided (unchanged), or when each match is vouched for at its own level. An Event match is vouched for by its Event's Organizer, under the same actors and conflict rule as disputes. A self-reported match is vouched for by one player of each team. When both matches are vouched for, the flag is resolved as approved. Each vouch is its own append-only row with an Audit Event. A hold still open after `MATCH_DUPLICATE_HOLD_DAYS` (default 14, never empty) voids the newer match (the flag's `match`). That releases the earlier one, so the first recorded result stands, as with an exact duplicate. The rule applies only when the earlier match (the flag's `duplicate_of`) is confirmed: a hold against a pending or disputed earlier match never voids the newer one (project owner, 2026-11-25, #268). Otherwise a player's pending, wrong self-report could get an Organizer's confirmed result voided.
- **Telling the people who can act.** When a match is disputed or held, the players and the Organizer who can settle it are told. The other participants' active Accounts and the Organizer's members get an email, and each can list the open disputes and holds they can act on. Emails follow the email rules: no other player's name and no text another person wrote. A dispute has no text (ADR 0015).
- **Signal flags and player checks.** Flags that are signals only (#206, #207, #175, #51, #123, #209) go to Organizers through the organizer flag feed (#213), not to a moderator review queue. An Organizer acts on them only within its own Events, for example in deciding who plays in which division. Seed checks move to Organizers (#50), as the roadmap's "confirm starting ratings" says, and #50 records the details in its own ADR. A flag still never changes a rating, holds a match, or limits a player.
- **When the Organizer is the problem.** A dispute about an Event match goes to its Organizer, which may be the party at fault. A player can report an Organizer to OPEN Moderators. Moderators see each Organizer's reports, its dispute counts (opened, withdrawn, resolved each way, expired), and its signal flags. They decide only about the Organizer: keep it verified, or unverify it and void its matches in bulk (ADR 0032). This is organizer approval, not match review.
- **Unchanged.** A dispute stops a match counting (ADR 0013). The Ledger is append-only. Every status change goes through `apps.matches.workflow`, with its lock order, Audit Events, and signals. Permission rules stay in `apps.matches.permissions`. No flag applies a penalty.

## Consequences

- No match waits on a moderator. Every dispute and hold ends within a known time.
- Any participant can keep a match out of ratings by disputing it and never withdrawing, unless an Organizer confirms it. For a self-reported match this is already the rule: it counts only when both teams agree. For an Event match, the Organizer has the dispute period to act. Repeated disputes by one player are a candidate signal for #213.
- A player can hold an Event match for up to `MATCH_DUPLICATE_HOLD_DAYS` by self-submitting a near duplicate. The Organizer can vouch for its match, but the hold ends only when the player's side vouches or the period ends. When the player's match came first and is still pending or disputed, the period does not end the hold: it lasts until a vouch or a void (such as the pending match's own expiry, when `MATCH_CONFIRMATION_EXPIRY_DAYS` is set). Whether a pending match should hold another match at all is a question for a later issue.
- An Organizer that does nothing lets its disputes void, so Organizers that run Events need to watch their list. The notices make that possible.
- A legacy club-only match (ADR 0021), or a match whose Event's Organizer is no longer verified, has no Organizer level. Its dispute ends by withdrawal or the dispute period.
- Organizer members gain the power to confirm a disputed match. That is score confirmation, a security-sensitive area (hard rule 8). The checks are the conflict rule, the required reason, the Audit Event, and the moderators' view of each Organizer's counts.
- Once the lower levels exist, the moderator match actions in the admin (approve, dismiss flags, resolve dispute, edit, void) are removed, along with `approve_match`, `resolve_flags`'s moderator path, and the moderator branches of `resolve_dispute`, `void_match`, and `edit_match`. The match admin stays read-only, for looking into an Organizer. The bulk void stays. ADR 0024's rule that sends every participant as a confirmer for a self-reported match a moderator confirmed has nothing left to apply to. Nothing has been deployed, so no stored match needs it.
- The work is epic #266: withdrawal and player corrections (#267), dispute and hold expiry (#268), Organizer dispute resolution (#269), vouching (#270), reporting an Organizer (#271), notices (#272), and removing moderator match review (#273). `CONTEXT.md` (Confirmation, Dispute, Flagged Match, Correction) and `CLAUDE.md` change in those issues.

## Alternatives considered

- Keep moderators as the final level for disputes and flags. Rejected: OPEN has no paid staff, and a queue nobody works holds results forever.
- Let an unsettled dispute lapse to the recorded result. Rejected: a submitter could ignore every dispute, and a contested result would move ratings.
- Let an unsettled duplicate hold lapse with both matches counting. Rejected: the same match could count twice by waiting. Voiding the newer one keeps the first result, as the exact duplicate rule does.
- Let a player edit a confirmed self-reported match directly. Rejected: disputing first already stops it counting, and the correction then needs the other team's confirmation.
- Let Organizers act on any player's flags. Rejected: an Organizer acts only within its own Events (#213).

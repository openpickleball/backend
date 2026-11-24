# 0021. Organizers are the only trusted submitters

- Status: Proposed
- Date: 2026-11-23

## Context

A match from a trusted submitter is confirmed when it is recorded (decision F4, ADR 0013), so who counts as trusted decides who may move ratings (CLAUDE.md, hard rule 8). ADR 0012 gives that trust two ways:

- Event authority: a member of the Event's Organizer while it is verified, or an approved application that Organizer owns.
- Club authority: any Club Role at the Club (admin or match manager), or an approved application the Club granted.

Organizers are verified by a moderator (#40). Clubs are not. So any Account given a Club Role, and any application a Club granted, records confirmed matches with no moderator review. A match that names a Club and no Event needs only club authority, so a Club can record confirmed matches that no verified Organizer stands behind.

Clubs, organizers, and events are also easy to confuse. The external systems OPEN's partners already use speak of "club directors" and "tournament directors" as kinds of submitter. In OPEN, those are people acting for an Organizer, and the club or tournament is what they run. The project owner decided on 2026-11-23 that OPEN has one trusted submitter, the verified Organizer, instead of also verifying Clubs.

## Decision

This amends ADR 0012 (submission and change rules) and ADR 0013 (the duplicate path's confirmation).

- **Who:** an Organizer is the only party trusted to submit matches. Its trust comes from moderator verification. A Club is where or with whom people play. An Event is what matches belong to, and it sets the Match Type.
- **Accounts:** an Account has submission authority only as an Organizer Member (either role) of a verified Organizer. A Club Role gives no submission authority.
- **Applications:** an approved application acts for an Organizer when that Organizer owns it (`PartnerApplication.organizer`) or has granted it an Organizer Application Grant. An admin Organizer Member makes and removes grants. This replaces the Club Application Grant, so a platform that serves many Organizers holds one grant from each. An application has no other path to submit.
- **Every trusted match has an Event.** A match with a Club needs an Event too. A match with neither is still a player's self-submission (decision F1), unchanged. A match with a Club and no Event is refused, because no Organizer stands behind it and it has no Match Type.
- **A Club decides who files under its name.** A match or Event that names a Club needs a Club Organizer Grant from that Club to the Event's Organizer. An admin Club Role makes and removes it. The grant gives consent, not trust: the Organizer must still be verified.
- **Club Roles:** a Club has only the admin role. A Club admin manages the Club's profile and its Club Organizer Grants. The match manager Club Role is removed.
- **Changes:** a match may be changed by the approved application that submitted it, or by a member of its Event's verified Organizer. A Club admin may not. Moderators are unchanged (ADR 0013).
- **Confirmation:** the duplicate path of `record_match()` confirms a pending match only for a submitter with event authority, under the same rules. Club authority no longer exists, so it no longer confirms.
- Unchanged from ADR 0012: the rules live in `apps.matches.permissions`, `record_match()` checks them before any validation or lookup, a refusal is the same fixed 403, every rule reads from the database by id, and no rule reads payment status (hard rule 7).

## Consequences

- One verification path to secure: a moderator verifies Organizers (#40). Clubs need no verification flow.
- A club that runs its own leagues or ladders becomes, or works through, an Organizer. Its staff are Organizer Members, and its leagues are Events at the Club. A person who organizes alone is an Organizer with one member, as before.
- "Linking a platform with OPEN" means an Organizer granting the platform's application, not a Club granting it.
- Casual play that club staff want to record as confirmed goes in an Event of kind `rec_verified`. A player can still self-submit any match.
- A verified Organizer's Event cannot claim a Club without that Club's consent. A Club that removes a grant stops new matches and Events under its name; matches already recorded stay in the Ledger (hard rule 3).
- The code, the `ClubRole` choices, the `ClubApplicationGrant` model, `CONTEXT.md`, `CLAUDE.md`, and the personal data inventory change in the issue that carries this out. Nothing has been deployed, so there is no data to move.
- Every grant and role change is a permission change in a security-sensitive area, so each records an Audit Event (#158 widens to cover the new grants).
- A verified Organizer carries all trusted submissions, so abuse by one is handled by review signals and cleanup (#205, #206, #208), not by a second kind of trust.

## Alternatives considered

- Verify Clubs too, with their own `verified_at` and moderator flow. Rejected: two verification systems to secure and keep in step, and the same person often runs both.
- Keep club authority without verification (ADR 0012 as it was). Rejected: it lets an unreviewed party record confirmed matches.
- Event authority alone for a match that names a Club, with no consent from the Club. Rejected: an Organizer could file matches under any Club's name.
- Keep the Club Application Grant beside the new Organizer Application Grant. Rejected: two ways to link a platform, and the club path skips verification.

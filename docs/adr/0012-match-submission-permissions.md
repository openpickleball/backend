# 0012. Match submission permissions

- Status: Proposed
- Amended by: [ADR 0021](0021-organizers-are-the-trusted-submitters.md) (the verified Organizer is the only trusted submitter: club authority and the Club Application Grant are gone, an application also acts for an Organizer that granted it an Organizer Application Grant, a match with a Club needs an Event and the Club's Club Organizer Grant to its Organizer, and a Club admin no longer changes matches)
- Amended by: [ADR 0043](0043-mfa-totp-and-recovery-codes.md) (a member's event authority also needs MFA on the member's Account)
- Date: 2026-11-15

## Context

Once #26 lands, a match that a permitted Organizer, Club, or application submits will be confirmed on creation (decision F4 in #1), so who may submit a match decides who may move ratings. Who may change a match matters as much. Both are security-sensitive (CLAUDE.md, hard rule 8). Organizers, Clubs, partner platforms, and players will all submit through different paths: the first-party endpoints (#31), the partner endpoints (#32), CSV upload (#71), and bulk partner submission (#72). Issue #79 asks for one set of rules that every path checks the same way.

## Decision

- The rules live in `apps.matches.permissions`: `can_create_match(actor, submission)` and `can_change_match(actor, match)`. An actor is an Account or a Partner Application. An Account counts only when it is saved and active. An application counts only when it is approved. Sandbox status and scopes are not checked here: #74 and #28 own them.
- Event authority: a member, with either role, of the Event's Organizer while that Organizer is verified, or an approved application that verified Organizer owns (`PartnerApplication.organizer`).
- Club authority: any Club Role at the Club, or an approved application with a Club Application Grant from it.
- A match with an Event needs event authority. A match with a Club needs club authority. A match with both needs both, so an Organizer cannot file matches under a Club that has not given it a role or a grant.
- A match with no Event and no Club is a player's self-submission (decision F1): only an active Account whose Player plays in the match may submit it. An application never may, even when approved.
- A match may be changed only by the approved application that submitted it, by a member of its Event's verified Organizer, or by an admin of its Club. A Club match manager may submit club matches but not change them. An application never changes another application's match, whatever its scopes or grants. Moderators act through #26's change service from #40's admin, which records a reason and an Audit Event, not through these rules.
- `record_match()` checks every submitter it is given before any validation: before score validation, model validation, duplicate lookups, locks, or writes. A refusal raises `MatchPermissionDenied`, which the API returns as a 403 `ErrorResponse` with code `forbidden`, fixed text, and empty `errors`. The text never says which rule failed, so a refused submitter cannot learn whether an Organizer is verified or whether a match already exists.
- Verification, approval, roles, grants, and an Account's active status are read from the database by id, never from the instance passed in, so a stale instance cannot pass.
- No rule reads payment status or paid entitlements (hard rule 7).

## Consequences

- Every submission path gets the rules by calling `record_match()`. A new rule goes in `apps.matches.permissions`, never in a path.
- The checks take no row locks. A role, grant, verification, approval, or active status removed while a submission is in flight can let that one submission through. Locking role rows would serialize unrelated submissions.
- Nothing checks that a match's Club is its Event's Club, or that it was played within the Event's dates. A verified Organizer's member can submit a match for its Event with no Club, even when the Event is at a Club.
- A near duplicate is not tied to the submitter's own Club or Event. Any permitted submitter, for example a match manager at one Club, can send the players and date of another Club's or Event's match with other scores. That match is then held from counting until moderator review (ADR 0011). This narrows the risk ADR 0011 left to #79 to permitted submitters but does not remove it. #40 (review) and #26 or #44 (refining the hold) carry the rest.
- Any permitted submitter can still learn from a 409 that a match with given players, date, and scores exists (ADR 0011). A refused submitter cannot.
- Changing a match needs only one authority (Event or Club), while creating one with both needs both. #26 must call `can_change_match` from its change service.
- Only #29's service will write approval. Until it lands, tests approve an application through a test-only helper.

## Alternatives considered

- Event authority alone for a match with an Event and a Club. Rejected: the issue's acceptance criteria require a role or grant for any match with a Club, and a Club should decide who files matches under its name.
- Letting an approved application submit rec play with no Event or Club. Rejected: decision F1 keeps those for player self-submission, which stays pending until both teams confirm (or, since ADR 0013, until a permitted submitter sends the same match), while an application's match will be confirmed on creation (#26).
- Checking permission in each submission path. Rejected: paths would drift, and a path that checked after the duplicate lookup would leak whether a match exists.
- A 403 that names the failed rule. Rejected: it would tell any caller whether an Organizer is verified or a Club has granted an application.

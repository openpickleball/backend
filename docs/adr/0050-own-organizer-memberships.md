# 0050. An Account reads its own Organizer memberships

- Status: Proposed
- Date: 2026-11-26
- Amended by: [ADR 0054](0054-organizer-self-service.md) (memberships are no longer all made by staff: an Account creates an Organizer and invites members; each membership gains `verification_requested`)

## Context

The organizer operations (Events, results files, dispute settling) work for a member of an Organizer, but the web app could not learn whether the signed-in Account is a member, or of which Organizer (issue #301). `GET /api/v1/accounts/me` returns only the email, `list_organizer_events` is empty both for a non-member and for a member whose Organizer has no Events, and an Event create needs the Organizer's OPEN Code. Issue #301 asks for each membership's Organizer code, name, verified state, and role, either on `accounts/me` or as its own operation.

## Decision

- A new operation, `GET /api/v1/organizer/organizers` (`list_my_organizers`, `apps.clubs.api.organizer_router`), returns the signed-in Account's own memberships: the Organizer's OPEN Code (the value `EventCreateIn.organizer` takes), its name, `verified`, and the Account's role (`admin` or `match_manager`). It needs sign-in and uses the Account throttle. It is a plain list ordered by OPEN Code, empty for an Account with no memberships.
- It is its own operation, not fields on `CurrentAccount`. `/api/v1/organizer/` already holds a member's own operations, so the app's organizer area reads from one prefix. `accounts/me` stays the Account's own record, called on every page load, with no Organizer query, and `apps.accounts` stays free of an `apps.clubs` import.
- It reads only the Account's own membership rows, so it never names another member or another Account's memberships.
- The Organizer's name, an identifying field (ADR 0009), goes only to that Organizer's own members.
- `verified` is the Organizer's state, not whether the Account may act for it now. Organizer authority also needs MFA on the Account (ADR 0043); the response carries no flag for that, since the issue asks only for the four fields. The app learns whether the Account has MFA from allauth's MFA endpoints, and a refused action still gets its 403.

## Consequences

- The app shows an organizer area when the list is not empty, and can create an Organizer's first Event.
- A member of an unverified Organizer sees its name and code. Members were already told the Organizer's Events, verified or not.
- An authority flag, if the app later needs one, is a new field that reuses `apps.matches.permissions` and `apps.accounts.mfa` rather than deriving the rules again.

## Alternatives considered

- Memberships on `CurrentAccount`: one request fewer, but an Organizer query on every `accounts/me` call and a second place that answers organizer questions.
- Paging: an Account has few memberships, all made by staff, so a plain list is enough.

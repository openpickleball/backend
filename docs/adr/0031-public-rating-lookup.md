# 0031. Public rating lookup and signed-in rating history

- Status: Proposed
- Date: 2026-11-24

## Context

Issue #53: players, apps, and partners need to look up a player's current ratings and their history. The partner rating card (#96) and platforms that only display ratings call the lookup more than anything else, without credentials, so the issue asks for a public lookup. ADR 0030 already planned a public ratings operation keyed by client IP.

ADR 0002 puts profiles behind sign-in and shows a player's display name and current Ratings to every signed-in Account, minors included, with no opt-out. It left anonymous reads of ratings (#53) to a separate decision. Players aged 13 to 17 are left out of all partner sharing (ADR 0009). Privacy settings cover the other profile details, including rating history, and their most open level is "Signed-in players".

The engine is pinned at `v1.0.0`. The project still labels every rating as a demonstration rating (decision Q4 in #1, CLAUDE.md). OPEN is in Shadow Mode (CONTEXT.md): its Ratings are not yet published or relied on.

## Decision

- **The current ratings lookup is public.** `GET /api/v1/players/{code}/ratings` takes no sign-in and reads no cookie or token: a signed-in request gets the same answer. Its only throttle is the client IP throttle (ADR 0030). It sets no cookie.
- **Who it shows.** A player whose Account is active and who is 18 or older on the age limit date (`apps/players/age.py`, ADR 0017). The check fails closed: a missing birth year or month, or a stored age under 18 for any reason, hides the player. Every other player gets the same 404 as an unknown or malformed code. Current ratings have no privacy setting (ADR 0002), so none is read. The age is checked on every request, so a player shows from the last day of the month they turn 18 (the date at UTC-12, ADR 0017), whatever their privacy settings.
- **What it shows.** The OPEN Code, the public profile URL, and one entry per rating format the player has, keyed by the format's value. Each entry has the rating, its OPEN-earned reliability, the carried external reliability apart from it (hard rule 5, null when there is none), `provisional` (live updates not yet replayed, hard rule 3), `established`, the spec version, the last updated time, and `demonstration`. No display name, row id, match, or gender division field. A men's or women's doubles rating shows the player's gender division, as ADR 0009 accepts for ratings.
- **Rating history needs sign-in.** `GET /api/v1/players/{code}/rating-history` follows the player's `rating_history_visibility`, as match history follows its setting (ADR 0015): the player's own Account always sees it, another signed-in Account only at "Signed-in players" and only while the player's Account is active. Anyone else gets the same 404 as an unknown code. Players aged 13 to 17 start with it private. Items are newest first, paged by limit and offset, and carry no match, replay, or row id.
- **Reliability and the label.** No response has a rating without its reliability. The OpenAPI document says that the OPEN standard requires any app that shows an OPEN rating to show its reliability next to it. Every rating, and the lookup response itself, carries `demonstration: true` while the engine's output is labeled a demonstration rating.
- **The profile URL** comes from the `OPEN_PUBLIC_PROFILE_URL` setting, default `https://openpb.org/players/`, followed by the OPEN Code. Settings refuse a value that is not an absolute http or https URL whose path ends in `/`, or that has a query, fragment, or user info, and require https with DEBUG off.
- **Nothing reads payment status** (hard rule 7).

## Before the lookup goes public

The operation is mounted with no switch, so any deployment serves it. No deployment exists yet (#61). This is a release rule that nothing in the code enforces: no deployment exposes the lookup until each of these is settled:

- The `do-connecting-ip` header check on #61 passes (ADR 0030).
- The IPv6 bucket: a client holding more than one /64 gets one bucket per /64 (ADR 0030). Decide whether to shorten the prefix or add a coarser window.
- The Valkey eviction policy: each client IP adds up to two counter keys, and Valkey is also the Celery broker (ADR 0006). The deployed policy must never evict broker data (comment on #61).

## Consequences

- Anyone can read the current ratings of an active adult player whose OPEN Code they know. A code is not a secret (ADR 0008), so this is the same as publishing those ratings. Display names stay behind sign-in. While OPEN is in Shadow Mode, every rating it returns is labeled a demonstration rating.
- The 404 hides a player from someone who does not know the code exists. A signed-in Account that finds a minor through search (ADR 0018) gets a code that this lookup answers with 404, so it learns the player is not an active adult. By polling until the answer turns to 200, it learns the month the player turns 18, so their birth year and month. Accepted for now and open for the project owner: closing it needs search or profiles to stop handing out minors' codes, or an answer for minors that is not a 404.
- A men's or women's doubles rating shows the player's gender division to anyone, not only to signed-in Accounts and partners as ADR 0009 accepted. Open for the project owner.
- A player aged 13 to 17 who sets rating history to "Signed-in players" shows every signed-in Account their history, newest rating included, while this lookup hides their current ratings. The history follows the player's own choice, as match history does.
- Signed-in Accounts cannot yet read a minor's current ratings through the API, although ADR 0002 lets them. A later issue can add that to a signed-in operation.
- Responses are not cached yet (#54): every lookup reads the database.
- Rating history is not available to anonymous callers or partners. #83 decides partner access.
- The client IP enters throttle cache keys for every public lookup (personal data inventory).
- Amends ADR 0002: anonymous callers may read current ratings, for adults only.

## Alternatives considered

- **A public history at "Signed-in players".** Rejected: the player chose signed-in viewers, not everyone.
- **Hide a player's current ratings by a privacy setting.** Rejected by ADR 0002: events need them.
- **Gate minors by their privacy settings instead of their age.** Rejected: a minor could open their settings, and partner sharing already excludes minors by age.
- **Answer a signed-in request with ADR 0002's wider view (minors included).** Rejected for now: the response would depend on a cookie, which the card (#96) and caching (#54) cannot allow.

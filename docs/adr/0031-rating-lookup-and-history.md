# 0031. Rating lookup and rating history behind sign-in

- Status: Proposed
- Date: 2026-11-24

## Context

Issue #53: players, apps, and partners need to look up a player's current ratings and their history. The issue first asked for a public lookup with no sign-in, and ADR 0030 planned a per-IP throttle for it.

ADR 0002 puts profiles behind sign-in. It shows the display name and current Ratings of every Player whose Account is active to every signed-in Account, players aged 13 to 17 included, with no opt-out. ADR 0009 repeats this for players aged 13 to 17 and keeps them out of partner sharing. Privacy settings cover the other profile details, rating history among them, and their most open level is "Signed-in players".

A first version of this change made the lookup public for adults only. Review found that it leaked a minor's age: a signed-in Account can find a minor's OPEN Code through search (ADR 0018), and polling the public lookup until 404 turns to 200 reveals the day the player turns 18, so their birth year and month. The project owner then decided (comment on #53, 2026-11-24) that the lookup needs sign-in, that nothing about a player is public without sign-in, and that the age leak must be fixed.

The engine is pinned at `v1.0.0`. The project still labels every rating as a demonstration rating (decision Q4 in #1, CLAUDE.md). OPEN is in Shadow Mode (CONTEXT.md).

## Decision

- **Both operations need sign-in.** `GET /api/v1/players/{code}/ratings` and `GET /api/v1/players/{code}/rating-history` use `account_auth` and the per-Account throttle (ADR 0030). The per-IP throttle stays in `config/throttling.py` for a later public operation, which needs its own decision (#96, partner lookups).
- **Current ratings follow ADR 0002.** Any signed-in Account sees the current Ratings of a Player whose Account is active, players aged 13 to 17 included. There is no privacy setting and no age check, so no answer changes when a player turns 18, and the lookup reveals no age. A player whose Account is inactive gets the same 404 as an unknown or malformed code.
- **What the lookup shows.** The OPEN Code, the profile URL, and one entry per rating format the player has, keyed by the format's value. Each entry has the rating, its OPEN-earned reliability, the carried external reliability apart from it (hard rule 5, null when there is none), `provisional` (live updates not yet replayed, hard rule 3), `established`, the spec version, the last updated time, and `demonstration`. No display name, row id, match, or gender division field. A men's or women's doubles rating shows the player's gender division to signed-in Accounts, as ADR 0009 accepts for ratings.
- **Rating history follows its setting.** The history follows the player's `rating_history_visibility`, as match history follows its setting (ADR 0015): the player's own Account always sees it, another signed-in Account only at "Signed-in players" and only while the player's Account is active. Anyone else gets the same 404 as an unknown code. Players aged 13 to 17 start with it private. Items are newest first, paged by limit and offset, and carry no match, replay, or row id.
- **Reliability and the label.** No response has a rating without its reliability. The OpenAPI document says that the OPEN standard requires any app that shows an OPEN rating to show its reliability next to it. Every rating, and the lookup response itself, carries `demonstration: true` while the engine's output is labeled a demonstration rating.
- **The profile URL** comes from the `OPEN_PUBLIC_PROFILE_URL` setting, default `https://openpb.org/players/`, followed by the OPEN Code. Settings refuse a value that is not an absolute http or https URL whose path ends in `/`, or that has a query, fragment, or user info, and require https with DEBUG off.
- **Nothing reads payment status** (hard rule 7).

## Consequences

- Issue #53's criterion that an anonymous request with no cookie gets the ratings, and that the response sets no cookie, no longer applies.
- The partner rating card (#96) and platforms that only display ratings cannot read ratings without sign-in. A public or partner read needs a new decision, which must avoid the age leak described above.
- A player aged 13 to 17 who sets rating history to "Signed-in players" shows every signed-in Account their history, as the player chose.
- Responses are not cached yet (#54): every lookup reads the database.
- The IPv6 /64 bucket and the `do-connecting-ip` check (ADR 0030, #61) still gate any later public operation, not these two.

## Alternatives considered

- **A public lookup for adults only.** Rejected: it reveals a minor's 18th birthday to anyone who knows the code, and search hands out codes.
- **Hide current ratings by a privacy setting.** Rejected by ADR 0002: events need them, and a player cannot hide them.
- **A public history at "Signed-in players".** Rejected: the player chose signed-in viewers, not everyone.

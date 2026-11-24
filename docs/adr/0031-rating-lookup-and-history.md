# 0031. Rating lookup, rating history, and a setting for current ratings

- Status: Proposed
- Date: 2026-11-24

## Context

Issue #53: players, apps, and partners need to look up a player's current ratings and their history. The issue first asked for a public lookup with no sign-in, and ADR 0030 planned a per-IP throttle for it.

ADR 0002 puts profiles behind sign-in and shows every active Player's display name and current Ratings to every signed-in Account, players aged 13 to 17 included, with no setting to hide them. ADR 0009 repeats this for players aged 13 to 17. Privacy settings cover the other profile details, and their most open level is "Signed-in players".

Two versions of this change were rejected in review:

- A public lookup for adults only leaked a minor's age. A signed-in Account can find a minor's OPEN Code through search (ADR 0018). Polling the lookup until 404 turned to 200 revealed the day the player turned 18, so their birth year and month.
- A signed-in lookup that showed every player, minors included, as ADR 0002 says. The project owner did not accept showing minors' current ratings.

The project owner decided (comments on #53, 2026-11-24): both operations need sign-in, nothing about a player is public without sign-in, minors' current ratings stay hidden, and no answer may change on an 18th birthday.

The engine is pinned at `v1.0.0`. The project still labels every rating as a demonstration rating (decision Q4 in #1, CLAUDE.md). OPEN is in Shadow Mode (CONTEXT.md).

## Decision

- **Both operations need sign-in.** `GET /api/v1/players/{code}/ratings` and `GET /api/v1/players/{code}/rating-history` use `account_auth` and the per-Account throttle (ADR 0030). The per-IP throttle stays in `config/throttling.py` for a later public operation, which needs its own decision (#96, partner lookups).
- **A privacy setting for current ratings.** `PlayerPrivacy.rating_visibility` has the two usual levels.
  - A player aged 13 to 17 when the Player is created starts at "Only me" and cannot widen it before 18 (`apps/players/age.py`).
  - Nothing changes at 18. The player widens it themselves through `update_privacy()`, which records the usual Audit Event. So the lookup's answer never changes on a birthday: it changes only when the player acts.
  - A player who is an adult when the Player is created starts at "Signed-in players".
  - Nobody can change it to "Only me": an adult cannot hide current ratings (ADR 0002), and a minor is already there. A former minor who never widens it stays hidden.
  - The model's default is "Only me", so a row made outside `create_player()` fails closed.
- **The lookup follows the setting.** The player's own Account always sees its ratings. Another signed-in Account sees them only at "Signed-in players" and only while the player's Account is active. Anyone else gets the same 404 as an unknown or malformed code.
- **What the lookup shows.** The OPEN Code, the profile URL, and one entry per rating format the player has, keyed by the format's value. Each entry has the rating, its OPEN-earned reliability, the carried external reliability apart from it (hard rule 5, null when there is none), `provisional` (live updates not yet replayed, hard rule 3), `established`, the spec version, the last updated time, and `demonstration`. No display name, row id, match, or gender division field. A men's or women's doubles rating shows the player's gender division to the signed-in Accounts that may see it, as ADR 0009 accepts for ratings.
- **Rating history follows its own setting.** The history follows `rating_history_visibility`, as match history follows its setting (ADR 0015). Items are newest first, paged by limit and offset, and carry no match, replay, or row id.
- **Reliability and the label.** No response has a rating without its reliability. The OpenAPI document says that the OPEN standard requires any app that shows an OPEN rating to show its reliability next to it. Every rating, and the lookup response itself, carries `demonstration: true` while the engine's output is labeled a demonstration rating.
- **The profile URL** comes from the `OPEN_PUBLIC_PROFILE_URL` setting, default `https://openpb.org/players/`, followed by the OPEN Code. Settings refuse a value that is not an absolute http or https URL whose path ends in `/`, or that has a query, fragment, or user info, and require https with DEBUG off.
- **Nothing reads payment status** (hard rule 7).
- **Amends ADR 0002 and ADR 0009.** Current Ratings are no longer always visible to every signed-in Account. They follow `rating_visibility`, which only a player created as a minor can keep at "Only me".

## Consequences

- Issue #53's criterion that an anonymous request with no cookie gets the ratings, and that the response sets no cookie, no longer applies.
- The partner rating card (#96) and platforms that only display ratings cannot read ratings without sign-in. A public or partner read needs a new decision, and must not reveal an age.
- Events and other players cannot see the current ratings of a player created as a minor until that player widens the setting at 18 or later. ADR 0002 rejected a setting that hides ratings because events need them; this exception covers only players created as minors.
- A player aged 13 to 17 who sets rating history to "Signed-in players" shows every signed-in Account their history, newest rating included, while their current ratings stay hidden. The history follows the player's own choice, as match history does.
- Responses are not cached yet (#54): every lookup reads the database.
- The IPv6 /64 bucket and the `do-connecting-ip` check (ADR 0030, #61) still gate any later public operation.

## Alternatives considered

- **A public lookup for adults only.** Rejected: it reveals a minor's 18th birthday to anyone who knows the code, and search hands out codes.
- **Show every active player's current ratings to signed-in Accounts, minors included (ADR 0002).** Rejected by the project owner.
- **Hide minors by age, and show them from 18.** Rejected: the answer would change on the birthday and reveal it.
- **Widen the setting automatically at 18.** Rejected for the same reason.
- **A public history at "Signed-in players".** Rejected: the player chose signed-in viewers, not everyone.

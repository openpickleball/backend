# 0018. Player search by display name

- Status: Proposed
- Date: 2026-11-18

## Context

Issue #191: the match form needs opponents and partners, and a match names each player by OPEN Code (ADR 0008, ADR 0015). Players do not know each other's codes, and the API had no way to find a player by name.

ADR 0002 makes a Player's display name visible to every signed-in Account, players aged 13 to 17 included, once Ratings are published. No Rating is published yet. ADR 0015 already shows display names in match responses before then, but only to an Account that can see the match. A search lets any signed-in Account find players it has never played with, so it needs its own decision. A search can also be used to list every player, so it needs limits.

## Decision

- `GET /api/v1/players/search` (`search_players`) takes `q` and `limit` and needs a signed-in Account (`account_auth`). Partners have no access to it.
- It is allowed before Ratings are published. It shows only what ADR 0002 will show every signed-in Account once they are: a Player's display name and OPEN Code. Each result holds those two fields and nothing else.
- Players whose Account is inactive never appear. Players aged 13 to 17 appear like any other player, as ADR 0002 sets out for their names. There is no privacy setting for it, since the display name has none (ADR 0002).
- `q` is stripped, then must be 3 to 64 characters, with no control characters or lone surrogates. Matching ignores case and works on word starts: the name starts with `q`, or a word in it does, where a word starts after a space or a hyphen. A fragment in the middle of a word does not match.
- `limit` is 1 to 20, default 10. There is no count, offset, or paging.
- Each Account may search 30 times a minute and 300 times a day. A request refused with 422 counts. A request refused by the minute limit does not count toward the day limit. The counters are fixed windows (a clock minute, and a UTC day) in the default cache (Valkey), keyed by the Account's id, never its email. Past a limit, the answer is 429 with code `throttled` and a `Retry-After` header.

## Consequences

- Players can pick opponents and partners by name in the match form.
- Any signed-in Account can learn the display name and OPEN Code of active players, minors included, before Ratings are published. One Account can see at most 300 searches of 20 names in a UTC day. Fixed windows allow about twice a limit across an edge: about 60 searches within seconds across a minute edge, and about 600 within about 20 minutes across midnight UTC, since the minute limit still applies.
- A player whose whole display name is shorter than 3 characters cannot be found. A word shorter than 3 characters is found only by a query that runs on into the next word.
- A word that follows something other than a space or hyphen (an apostrophe, a no-break space) is not a word start, so a query that starts there does not match. "Bri" does not find "O'Brien".
- Display names are stored as typed, without Unicode normalization, so a name typed in one form (NFC or NFD) is not found by a query in the other.
- Once the day limit is reached, further requests still count toward the minute limit, so a refusal can carry a minute-long `Retry-After` while the day stays used up.
- The counters live in Valkey. If Valkey is down, search fails. Clearing the cache resets them.
- The search reads the whole Player table with `UPPER(display_name) LIKE`. That is fine at today's size; an index is a later change if it is needed.
- Search has its own throttle, with its rates in code, and stays out of #34's plan for first-party operations (`AuthRateThrottle`, rates from settings). Adding a second throttle to search would bring back the cross-counting described under Alternatives considered. #34 should weigh those problems for the other operations. No per-IP limit on search is planned.

## Alternatives considered

- **Wait until Ratings are published.** Follows ADR 0002 to the letter, but the match form could not use search until then, and players would have to share codes by hand.
- **Leave players aged 13 to 17 out of search.** Narrower, but ADR 0002 already makes their names visible to signed-in Accounts, and it would make them the only players another player must ask for a code.
- **Match a fragment anywhere in the name.** Simpler, but a 3-letter fragment matches many more players, which makes listing players easier.
- **A privacy setting for the display name.** ADR 0002 rules out hiding the display name.
- **ninja's built-in throttles.** `AuthRateThrottle` keys on a hash of `str(request.auth)`, the Account's email, so its key changes when the email does. ninja's throttles keep a timestamp list with a non-atomic read and write, so parallel requests can exceed the limit. Two of them on one operation also count a request one refuses against the other.

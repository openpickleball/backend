# 0015. First-party match endpoints

- Status: Proposed
- Date: 2026-11-17

## Context

Issue #31 adds the first API operations that players use on matches: submit a match they played, confirm or dispute it, and read a player's match history. The services already exist: `record_match()` (ADR 0011, ADR 0012) and the confirmation workflow (ADR 0013). What the endpoints show, to whom, and how they refuse were open.

Issue #31 asked to show a private player as "Private player" in other players' match history. ADR 0002 says that label no longer applies to a player's name: a display name is visible to every signed-in Account while the player's Account is active. ADR 0011 left #31 to decide how its 409 treats a duplicate, and ADR 0013 left #31 to decide where dispute text lives. ADR 0008 says no response reveals whether a record exists.

## Decision

### Operations

- `POST /api/v1/matches/` (`create_match`) is a player's self-submission (decision F1): no Event, no Club, no external id, location, or time. It takes the format, the date played, both teams as Player OPEN Codes, the game scores, and optionally best-of, scoring type, and game target, with `validate_match()`'s defaults (decision F15). The match is created pending through `record_match()` and needs a confirmation from each team.
- `POST /api/v1/matches/{code}/confirm` (`confirm_match`) and `POST /api/v1/matches/{code}/dispute` (`dispute_match`) call the workflow services. A dispute takes no body, so no dispute text is stored anywhere.
- `GET /api/v1/matches/{code}` (`get_match`, issue #194) returns one match by its OPEN Code, for a match detail screen or a link to a match.
- `GET /api/v1/players/{code}/matches` (`list_player_matches`) pages a player's matches by limit and offset, newest first.
- `GET /api/v1/me/matches/awaiting-confirmation` (`list_matches_awaiting_confirmation`, issue #192) pages, the same way, the pending matches the signed-in Account's own Player played in where no player on their team has confirmed. A match a teammate confirmed is left out, since one confirmation per team is enough. An Account with no Player gets an empty page.
- All of them take `config.auth.account_auth`.

### Who sees what

- In a match history, the player's own Account sees all of its matches, in every status. Another signed-in Account sees only confirmed matches, and only while the player's `match_history_visibility` allows it (`PlayerPrivacy.visible_to`, which also hides everything while the player's Account is inactive).
- A single match (`get_match`) is shown only to its participants, in every status: an active Account whose Player played in it (`participant_player`, the rule confirm and dispute use). Nobody else reads it there, whatever the players' privacy settings: not another signed-in Account, not a member of the match's Event Organizer, and not a moderator (moderators will use #40's admin). This is not a secrecy boundary. Another Account can see a confirmed match's code and contents in a visible match history, and a member of the match's verified Event Organizer gets the whole match back from confirm: a pending match they confirm, a confirmed match unchanged, and a 409 for a disputed or voided one, which tells them only that it is one of the two. Widening the read is its own decision.
- Every response shows each participant's display name and OPEN Code, whatever that participant's own privacy settings. A participant whose Account is inactive is shown as "Private player" with no code, except to that participant's own Account. ADR 0002 guarantees a name only while the Account is active, so this record hides it otherwise.
- Responses show no row id, submitter, flag, or confirmation row.
- Every match response also carries fields derived for the signed-in Account (#193). `my_team` is the viewer's own team, and `my_decision` their own latest decision (`confirm`, `dispute`, or `none`). `confirmed_by_team` (`a` and `b`, booleans only) says whether a player on each team has confirmed; a dispute is not a confirmation. A viewer whose Player did not play in the match gets a null `my_team`, `none`, and a null `confirmed_by_team`, so a stranger never learns who confirmed. `created_at` is when the match was submitted. None of these shows a confirmation row or names a player.
- These fields read only the confirmation rows the workflow records, and a row is never withdrawn: a player's confirmation still counts in `confirmed_by_team` after they dispute. A repeated decision is not recorded again, and a confirmation by an organizer or a moderator, or a correction, records no player decision. So `my_decision` can differ from what a request just asked for, and a confirmed match can show `false` for both teams. Clients read `status` for whether a match is confirmed. A participant can sometimes tell who confirmed: their singles opponent, or their own partner when they have no confirmation of their own.

### How the endpoints refuse

- A signed-in Account whose own Player is not on either team, or that has no Player, gets `MatchPermissionDenied`'s 403 before any code lookup, score validation, or duplicate check. Errors about the request's own shape, from schema parsing, come first.
- A team code that is malformed or names no Player gets a 422 item at `body.team_a.<i>` or `body.team_b.<i>` with code `unknown_player` and one fixed message.
- `MatchValidationError` returns 422 with one `errors` item per issue. In `create_match`, an issue about a request field is located in the body (`body.games.0`), like pydantic's own errors.
- A refused actor on an operation with a code in its path gets the same 404 as an unknown or malformed code, byte for byte: a confirm or dispute the workflow refuses (`MatchActionDenied`), a single match read by anyone who did not play in it, and a history the viewer may not see. This keeps ADR 0008's rule that no response reveals whether a record exists, for codes in a path. A team code in a submission body is the exception: its `unknown_player` item tells the submitter whether the code names a Player. `MatchActionDenied` still has a global 403 handler for other callers.
- `MatchStateError` returns 409 with code `conflict` and fixed text, for example a confirmation of a disputed or voided match. Only an actor the workflow allows reaches it: a participant, or for confirm, a member of the match's verified Event Organizer (ADR 0013). Each already knows the match.
- A duplicate returns ADR 0011's 409. A self-submitter is always on the match it duplicates, so the 409, and the code it names, tell only a participant about a match they played. This answers ADR 0011's question for #31. #32 still decides it for partners.
- Teams take at most 10 codes and games at most 20 scores, with fixed messages that never state the submitted count. The history `offset` stays within Postgres's bigint range. Both return 422, never a 500.

### Authentication

- `account_auth`'s session path returns 401, not ninja's CSRF 403, when the request has no signed-in user. A signed-in session still needs a valid CSRF token on an unsafe method, and gets 403 without one. Confirm and dispute document that 403.
- `request.auth` is the Account itself on both paths, never Django's lazy `request.user`, so views pass it straight to the match services.

## Consequences

- A player whose match history is private still appears by name in the history of an opponent or partner whose history is visible. Hiding them there would hide a display name, which ADR 0002 rules out.
- Display names appear in match history before any Rating is published. ADR 0002 makes them visible once Ratings are published and does not forbid this earlier.
- A submitter can name any Player by OPEN Code, including one whose Account is inactive, and can tell from `unknown_player` whether a code names a Player. The match then holds `PROTECT` rows for that Player. Whether new matches may name a Player whose Account is inactive or deleted is #116's open question (ADR 0002).
- A refused confirm, dispute, or single match read can run a few more queries than an unknown code before its 404. The response bytes are the same.
- `create_match` returns 403 both for a refused submitter and for a signed-in session without its CSRF token. The `message` tells them apart.

## Alternatives considered

- **"Private player" for anyone with a private match history.** It hides a display name, which ADR 0002 rules out.
- **A single match readable by other signed-in Accounts under the players' history privacy.** A confirmed match would be shown to anyone who can see one participant's match history. It needs a rule for which participant's setting governs, and the history already serves that case. Participants only is simpler and can be widened later without breaking clients (#194).
- **403 for a refused confirm, dispute, single match read, or history read.** Clearer to clients, but it tells any signed-in Account which match and player codes exist (ADR 0008).
- **Other viewers see every match that is not voided.** It would show pending and disputed results that the other team has not agreed to.

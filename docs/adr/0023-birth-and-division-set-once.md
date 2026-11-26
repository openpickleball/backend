# 0023. Birth year and month and gender division are set once

- Status: Proposed
- Date: 2026-11-23

## Context

ADR 0009 lets only the player set their own profile fields, and others only remove them. The Player update endpoint (`PATCH /api/v1/me/player`, #124) let the player change their birth year and month at any time, and ADR 0019 let them change the gender division between `mens` and `womens`.

The birth year and month decide the 13-and-older rule, the 13-to-17 privacy defaults, and the Age Group. The gender division decides which Rating Formats a player can enter. A player who can change them after the Player exists can step around signup's age screen, move between age bands, or move between the men's and women's Rating Formats. Issue #225 found one such gap: a change to an under-13 age on PATCH was refused but did not flag the session the way signup does (ADR 0017).

The project owner decided on 2026-11-23 (issue #225) that these fields are set once.

## Decision

This amends ADR 0009 (only the player sets these fields, and now only once), ADR 0017 (`update_profile()` no longer changes the birth year and month), and ADR 0019 (the player can no longer change the gender division).

- The birth year and month and the gender division are set once, when the Player is created: from the details held at signup, or from the create body when the session holds none (ADR 0017, ADR 0019).
- After that, the player cannot change them. `PATCH /api/v1/me/player` refuses each of the three fields with a 422 on that field (code `set_once`, "Your birth year and month can't be changed." or "Your gender division can't be changed."), whatever the value: a new one, the stored one, or null. The message never repeats the value. The rest of the request changes nothing. The OpenAPI document no longer lists the fields for PATCH.
- `update_profile()` refuses them the same way (`SET_ONCE_FIELDS` in `apps/players/models.py`), so no other path can change them either.
- With no birth change on PATCH, an under-13 age there has nothing to flag. Issue #225's PATCH gap is closed this way.
- Removal by others is unchanged: #190 and #116 may still remove these fields.
- A player who entered a wrong birth year and month or gender division deletes their Account and signs up again (#116). There is no correction path.

## Consequences

- The web and Expo apps must not offer to edit these fields after the Player exists.
- A Player stored with an empty gender division (before ADR 0019, or after a removal) cannot set one. Nothing with an empty value was deployed.
- A correction needs Account deletion (#116) first. ADR 0048 makes that path work: after `DELETE /api/v1/accounts/me` the same email signs up again and creates a new Player, even when the old Player stays in the Ledger.
- If a correction path is needed later (for example, staff fixing a value with evidence and an Audit Event), a new ADR must add it.

## Alternatives considered

- **Keep them editable, and flag the session for an under-13 change.** It closes the flag gap but keeps the way around the age screen and the move between age bands and Rating Formats.
- **Accept the stored value when it is sent again.** Kinder to a client that resends the whole profile, but the endpoint's GET body cannot be sent back as is anyway (it has `code`), and a field that accepts some values and not others is harder to document.
- **A staff correction path now.** Not needed before launch. It can come with its own ADR.

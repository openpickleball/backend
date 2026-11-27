# 0062. The display name is built from the signup names

- Status: Proposed
- Date: 2026-11-27

This amends [ADR 0009](0009-personal-data.md) and [ADR 0022](0022-name-and-place-at-signup.md), and answers item 2 of #323 (and the name part of item 3).

## Context

ADR 0022 made "First Last" the legal name and an adult's starting display name, and "First L." the starting display name of a player aged 13 to 17, and let the player change the display name later. ADR 0009 let the player change the legal name too. `PATCH /api/v1/me/player` took both as free text. The Player kept only the joined legal name; the first name lived only in the signup hold.

Issue #323 asked whether "First name and last initial" should be a setting the backend applies everywhere, or a rule on what display names may be. The project owner decided on 2026-11-27 (issue #354):

- The shown name is built only from the first and last name given at signup. Players cannot edit the display name or the legal name.
- 18 and older: "First Last" by default. A privacy setting lets the player show "First L." instead, and switch back.
- 13 to 17: always "First L.". The setting cannot change it while the player is 13 to 17.
- At 18 nothing changes on its own. The name stays "First L." until the player switches it.

## Decision

- **Storage.** The Player gets `first_name` and `last_name`, each an `EncryptedCharField` (ADR 0046) of at most 64 characters, set once by `create_player()`. `legal_name` stays a stored, encrypted field, set once to "First Last" at create. Keeping it is the smallest change: the admin's audited reveal, the data export, the own profile, and the inventory keep reading it as before, and nothing has to derive it. It holds the same data as the two names, so it adds no new kind of personal data. The two name columns allow NULL, so adding them needs no encryption key while the table is empty (ADR 0046), and NULL marks a Player whose names are not known: one stored before this change whose legal name did not split, or one whose personal data was removed.
- **The setting.** `PlayerPrivacy.name_format` is `full` ("First Last") or `first_last_initial` ("First L."). Its model default is `first_last_initial`, so a row made outside `create_player()` fails closed. `create_player()` sets `full` for an adult and `first_last_initial` for a player aged 13 to 17, with the other privacy defaults. It sits with the privacy settings in `PrivacyIn` and `PrivacyOut`, but it is not a visibility level, so it is not in `PlayerPrivacy.SETTINGS`; `PlayerPrivacy.FIELDS` lists both.
- **The display name.** `display_name` stays a stored column, so search (ADR 0018) and every match, club, and organizer view keep reading it. `apps/players/names.py` builds it: "First Last" for `full`, and for `first_last_initial` the first name, a space, the last name's first character that renders with the combining marks after it (ADR 0022's rule), and a period. `update_privacy()` rebuilds it from the stored names whenever `name_format` changes, in the same transaction. A Player with no stored names keeps its display name. Search matches the shown name only, so a "First L." player is not found by their full last name.
- **The lock.** `update_privacy()` refuses `full` while the player is 13 to 17, judged with `apps/players/age.py` at the time of the request, with a 422 on `body.privacy.name_format` (code `invalid`, "Your full last name can be shown only from age 18."). The stored value is always accepted, as for `rating_visibility`. A Player whose birth year or month was removed counts as locked. `MyPlayerOut.name_format_locked` tells the app whether the lock applies now, so the app never works out an age. Nothing changes the setting at 18.
- **No typed names.** `PATCH /api/v1/me/player` refuses `display_name` and `legal_name` as it refuses the set-once fields (ADR 0023): a 422 on that field, code `set_once`, "Your display name can't be changed." or "Your legal name can't be changed.", whatever the value, and the OpenAPI document no longer lists them for PATCH. `update_profile()` refuses them the same way. The first and last name cannot be sent there either (unknown keys).
- **The create body.** When the session holds no signup details, `POST /api/v1/me/player` takes `first_name` and `last_name`, both required (a 422 `missing` on each), in place of `display_name` and `legal_name`, and checks them with signup's rules (`PlayerNamesForm` in `apps/accounts/forms.py`): stripped, a character that renders, no control character or lone surrogate, a first name of at most 61 characters, and "First Last" within 64. An under-13 or future age still refuses the session first (issues #220 and #225).
- **Audit.** A `name_format` change is a privacy change: one `privacy.change` event with the old and new formats, like the other settings' levels. It never holds a name, and the rebuilt display name records no profile event.
- **Existing rows.** Migration `players.0012_fill_player_names` gives each Player the format `create_player()` would give it now (`full` for an adult, `first_last_initial` otherwise). Where the legal name splits cleanly at its first space (two names that render, a first name of at most 61 characters, and "First Last" within 64), it fills the names and rebuilds the display name; otherwise it leaves the names NULL and the display name as it is. Nothing was deployed with real players.
- **Removal.** Account deletion and the under-13 removal set both names to NULL and `name_format` to `first_last_initial`, with the other fields they clear.

## Consequences

- A player sees their name only as the signup gave it. A typo in a name needs Account deletion and a new signup, as for the birth year and month (ADR 0023). If a correction path is needed later, a new ADR must add it.
- The app shows a choice between "First Last" and "First L.", disabled while `name_format_locked` is true (openpickleball/frontend#328).
- An adult created before this change gets "First Last" back when the legal name splits, even if they had changed their display name. Accepted: nothing was deployed with real players.
- A split at the first space reads "Mary Ann Lee" as first name "Mary" and last name "Ann Lee". New Players never need a split: signup and the create body give the two names.
- The search opt-out and the other defaults for players aged 13 to 17 in #323 (hidden city and region, out of search) are not decided here.

## Alternatives considered

- **Derive the legal name from the two names.** One copy of the data, but it changes the admin reveal, the export, the inventory, and the column itself, for no gain in what is stored.
- **A rule on what display names may be (minors may only use "First L.").** Keeps free text, which the owner ruled out.
- **Build the display name when it is read.** No stored column to keep in step, but search, clubs, organizers, and match views all read the column today.
- **Change the format at 18.** The owner decided nothing changes on its own, as ADR 0031 does for the other settings.

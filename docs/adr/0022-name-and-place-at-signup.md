# 0022. First and last name and a place at signup

- Status: Proposed
- Date: 2026-11-23

## Context

Signup took the email, the password, the birth year and month (ADR 0017), and the gender division (ADR 0019). The name and the city and region were set only later, in the body of the Player create endpoint (`POST /api/v1/me/player`, #124) or through the profile endpoints. The Player has `display_name`, `legal_name`, `city`, and `region`, and no separate first and last name.

The project owner decided on 2026-11-23 (issue #221) that signup asks for a first name, a last name, and a location, so a new Player is complete from the start. The app suggests places as the player types, from Photon (an OpenStreetMap geocoder, on komoot's public instance), and sends only the chosen city and region, in the one signup request. Photon runs in the app, not the backend. The owner also decided that "First Last" is stored as the legal name and is the starting display name, which the player can change later, and that the city and region stay optional. Player search and match views show display names to every signed-in Account, players aged 13 to 17 included (ADR 0018), so the owner then decided that a player aged 13 to 17 starts with a shorter display name, "First L.".

## Decision

This amends ADR 0009 (the legal name is now set at signup) and ADR 0017 (signup holds more than the age).

- Signup on both headless clients takes `first_name` and `last_name`, required, and `city` and `region`, optional (`SignupAgeForm`, so allauth's OpenAPI document lists them). They are checked in the same step as the age and gender division, before allauth reads the email address. Each value is stripped. A name must have a character that renders. A value that is not a string, or that holds a control character or a lone surrogate, is refused. A first name has at most 61 characters, so "First L." fits the display name's 64 (the error goes on `first_name`), and "First Last" must fit those 64 characters (the error goes on `last_name`). A city or region has at most 100 characters. Each problem gets allauth's 400 with a field error (`param` set) and a fixed message that never repeats the value. Like a malformed age, it sets no refusal flag and holds nothing.
- A signup that passes holds `legal_name` ("First Last", one space between), `first_name` (so the last name's first character can be found), `city`, and `region` (empty when not given) in `open_signup_details`, with the age and gender division, under ADR 0017's rules: bound to the new Account, or to none for a taken address, so the answers for a new and a taken address stay alike. Logout removes them. An under-13 signup holds nothing.
- The Player create endpoint uses the held details for every value: the held legal name becomes the legal name, with the held city and region. The starting display name is the legal name for an adult. A player aged 13 to 17 starts with "First L.": the first name, a space, the last name's first character, and a period. The age is judged with `apps/players/age.py` (`is_minor`) when the Player is created, as `create_player()` judges it for the privacy defaults. Either can change the display name later. The body's values are not used while details are held. When the session holds none, the body gives them, and its `display_name` is required (a 422 `missing` on `body.display_name`), with the birth year and month and the gender division. `display_name` is otherwise optional in the body.
- The legal name stays as ADR 0009 sets it: returned only to the player, never in an Audit Event value or a log. Creating a Player records no Audit Event. Field-level encryption is still #115's.

## Consequences

- Every new Player made through signup has a legal name. ADR 0009 kept it optional, to be added only when an identity check needs it. It is now collected from every player who signs up. The database still allows an empty legal name (a Player made through the body, or one whose name was removed).
- The session holds the legal name, in plain text, until the Player is created, the player logs out, or the session ends. The inventory lists it.
- The app sends what a player types in the location field to Photon, a third party, before signup. OPEN stores only the chosen city and region.
- The web and Expo apps must send `first_name` and `last_name` with every signup (openpickleball/frontend#197).
- An adult's starting display name is the full name, so search and match views show it until the player changes it. A player aged 13 to 17 shows only the first name and the last name's initial, unless they change it. Nothing changes the display name at 18.
- Holds made before this change, with no name, are ignored, and the create endpoint asks for every value in the body. Nothing was deployed with them.

## Alternatives considered

- **Store first and last name as two fields.** ADR 0009 sets the Player fields, and a joined legal name serves identity checks. Two new fields would add identifying data the project does not need.
- **Let the body's display name win over the held name.** One rule, held details win, matches the age and gender division, and the player can change the display name afterward.
- **Make the city and region required.** The owner kept them optional, as ADR 0009 has them.
- **Geocode on the backend.** The app would send the typed text to OPEN, which would forward it. Photon in the app keeps the typed text out of OPEN's servers.

# 0019. Gender division required at signup

- Status: Proposed
- Date: 2026-11-23

## Context

ADR 0009 made the gender division optional: men's, women's, or empty. Signup asked only for the email, the password, and the birth year and month (ADR 0017). The Player create and update endpoints (#124) took the gender division as optional, and the player could clear it.

The gender division decides which Rating Formats a player can enter (men's, women's, and mixed doubles), and stats need it. The project owner decided on 2026-11-23 (issue #215) that signup requires it, with two choices: men's (`mens`) and women's (`womens`). There is still no raw gender field.

## Decision

This amends ADR 0009 (the gender division is required for every new Player) and ADR 0017 (signup takes it with the age).

- Signup on both headless clients takes a third required field, `gender_division`, `mens` or `womens` (`SignupAgeForm`, so allauth's OpenAPI document lists it as required). It is checked in the same step as the birth year and month, before allauth reads the email address. A missing or invalid value gets allauth's 400 with a field error (`param: "gender_division"`) and a fixed message that never repeats the value. Like a malformed age, it does not set the refusal flag. All three fields are checked before the age decision, so an under-13 age sent with a missing or invalid gender division gets the field error, not the refusal, and nothing is held.
- A signup that passes holds the gender division in the session together with the birth year and month, bound to the new Account, under ADR 0017's rules. The session key is now `open_signup_details`, read with `peek_signup_details()` and taken with `take_signup_details()` (`apps/players/signup.py`). A taken address holds it bound to no Account, so the answers for a new and a taken address stay alike. An under-13 signup holds nothing.
- The Player create endpoint (`POST /api/v1/me/player`) uses the held gender division with the held age, and ignores the body's. When the session holds no signup details, the body's `gender_division` is required, with the birth year and month.
- Every new Player has a gender division. `create_player()` requires it, and `Player.full_clean()` refuses an empty value. The player can change it between `mens` and `womens`, but cannot clear it.
- The database still allows an empty value. Removal of personal data (#190, #116) may empty it. A Player with an empty value can still change other fields; sending the stored empty value again is an unchanged value, so it changes nothing.

## Consequences

- The web and Expo apps must send `gender_division` with every signup. The signup form shows it as "Gender", with "Man" and "Woman".
- A player who does not fit either division cannot sign up. This is the project owner's decision, and a later ADR can revisit it.
- Holds made under the old `open_signup_birth` key are ignored, and the create endpoint asks for the details again. Nothing was deployed with the old key.
- A Player created before this change may have an empty gender division. Nothing fills it in; the player can set it.

## Alternatives considered

- **Keep it optional.** Rating Formats and stats need it, and an optional field leaves many players without one.
- **Ask for it only when the Player is created.** The frontend asks for it on the signup screen with the age, so holding it with the age keeps one flow and one set of session rules.
- **A third choice, or a raw gender field.** ADR 0009 rules out a raw gender field. Who may enter which division is a governance and event rule, not a backend rule.

# 0039. A password check before the rest of signup

- Status: Proposed
- Amended by: [ADR 0047](0047-account-security-limits-and-sessions.md) (the password rules also refuse a password found in a data breach, `password_breached`)
- Date: 2026-11-25

## Context

The app's signup has two screens (ADR 0020). Screen 1 asks for the email and password. Screen 2 asks for the age, the gender division, the name, the place, and the agreements. Everything is sent in one signup request at the end.

The app can check only the 8-character minimum itself. The other rules in `AUTH_PASSWORD_VALIDATORS` (too similar to the email, too common, entirely numeric) run only on the signup request. So a player learns that their password is too common only after filling in screen 2, and has to go back (issue #289).

allauth's headless signup also skips one of those rules. Its input checks the password with no Account, so Django's `UserAttributeSimilarityValidator` never runs, and signup accepts a password made from the email address.

## Decision

- `POST /_allauth/{client}/v1/auth/signup/password-check`, for both headless clients, takes `password` (required, a JSON string) and `email` (optional). `SignupPasswordCheckView` (`apps/accounts/headless_views.py`) checks, in the age check's order:
  1. allauth's signup rate limit;
  2. allauth's answers for a signed-in session (409) and for closed signup (403);
  3. the session's refusal flag: a refused session gets signup's fixed `signup_refused` answer, as from the age check and signup;
  4. the input, then the password rules.
- A password that passes gets `200` with `{"status": 200}`.
- A password that fails gets allauth's 400, `{"status": 400, "errors": [...]}`, with one entry per failed rule in the order of `AUTH_PASSWORD_VALIDATORS`. Each entry has `param` `password` and Django's message and code: `password_too_similar`, `password_too_short`, `password_too_common`, `password_entirely_numeric`. A missing, empty, or blank password gets the field's `required` error, and a value that is not a JSON string gets `invalid` ("Enter text."), both on `password`.
- The rules run through `clean_signup_password()`: allauth's own `clean_password()`, with an unsaved Account that holds only the address, so the whole of `AUTH_PASSWORD_VALIDATORS` runs, the similarity rule included. OPEN's signup input now calls the same function in its `clean()`, against the cleaned address. The check and signup give the same errors for the same password and address, and signup now refuses a password too similar to its email.
- The address is used only for the similarity rule. It is never validated or looked up. A string is normalized as signup normalizes it (`normalize_email_address`) and compared; a missing or non-string value is not compared, and the other rules still run. So the check finds no Account or `EmailAddress`, and its answer is the same whether or not an Account has the address, as signup's is.
- The check writes nothing to the session, on a pass or a fail. An app client that sent no token gets none. It sends no mail.
- It counts against allauth's `signup` rate limit, on the same counter as signup and the age check (allauth's default is 20 a minute per IP). A normal signup now costs three of the limit's requests, plus one for each password the player tries again.
- allauth's OpenAPI document lists it, with the age check (`apps/accounts/openapi.py`).

It sits next to the age check, under allauth's signup path, for ADR 0020's reasons: it works before an Account exists on both clients' sessions, uses allauth's error format, and shares signup's rate limit and its 409 and 403.

## Consequences

- The web and Expo apps call the check when screen 1 is submitted and show its errors on the password field. They still send the password with the signup request, which checks it again.
- The check and signup cannot drift: both call `clean_signup_password()`. Changing `AUTH_PASSWORD_VALIDATORS` changes both.
- A password made from the email address, which signup accepted before, is now refused at signup too.
- Players behind one IP share the signup rate limit, and the check adds requests to it. If that becomes a problem, the limit itself should change, for signup and both checks together.
- The check is not in `openapi/openapi.json`, which documents only the OPEN API at `/api/v1/`.

## Alternatives considered

- **Check the password in the app.** The app would need a copy of Django's common password list and the similarity rule, kept in step with the backend by hand.
- **Skip the similarity rule in the check, as allauth's signup does.** The check would pass passwords made from the address. Checking it in both places costs nothing and is what `AUTH_PASSWORD_VALIDATORS` asks for.
- **A separate rate limit for the check.** It would need its own setting kept in step with signup's. The check reveals nothing, but sharing the counter keeps one limit for the whole signup.
- **Validate the address in the check too.** Screen 1 could then show an email error early, but the check must not depend on the address beyond the similarity rule, and signup already reports a bad address.

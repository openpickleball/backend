# 0020. An age check before the rest of signup

- Status: Proposed
- Amended by: [ADR 0039](0039-signup-password-check.md) (a password check, `POST /_allauth/{client}/v1/auth/signup/password-check`, on the same rate limit counter, so a normal signup costs three of its requests)
- Date: 2026-11-23

## Context

ADR 0009 and ADR 0017 ask for the age first, on a neutral screen, so that nothing personal is collected from a child under 13. ADR 0017 put the age on the first signup screen, sent in the one signup request, and rejected a separate age endpoint.

The project owner decided on 2026-11-23 (issue #223) that the app's signup becomes two screens. Screen 1 asks for the email and password. Screen 2 starts with the birth month and year, then the gender division, the first and last name, the location (#221), and the agreement checkboxes (#222). Everything is still sent in one signup request at the end. The location goes to a third-party search, so the app must not let anyone type a name or a location until the age has passed. The app must never check the minimum age itself, so it needs the backend's answer before the rest of screen 2.

## Decision

This amends ADR 0017 (the app's screen order changes, and an age check comes before the rest of signup) and ADR 0009 ("signup asks for birth year and month first"). The age still comes before the other personal details that need it: the app lets no one type a name or a location until the check has passed. The signup request's own age screen is unchanged.

- `POST /_allauth/{client}/v1/auth/signup/age-check`, for both headless clients (`browser` with its session cookie and CSRF token, `app` with `X-Session-Token`), takes only `birth_year` and `birth_month` (`SignupBirthForm`, the same fields and messages as signup's). `SignupAgeCheckView` (`apps/accounts/headless_views.py`) checks, in signup's order:
  1. allauth's signup rate limit;
  2. allauth's answers for a signed-in session (409) and for closed signup (403);
  3. the session's refusal flag;
  4. the input;
  5. the age, with `is_old_enough()` (the end-of-month rule at UTC-12, ADR 0017).
- 13 or older gets `200` with `{"status": 200}`, and the session is not written. An app client that sent no token gets none. Signup details already held in the session stay as they were. The signup request still sends the age and checks it itself, so a pass grants nothing.
- Under 13 gets signup's fixed 400 (`signup_refused`, "We can't create an account with these details."). `refuse_signup()` flags the session (`open_signup_refused`) and drops any held signup details. An app client that sent no token gets one with the refusal, so its later requests carry the flag.
- A flagged session gets that refusal from the check and from signup, whatever it sends, before its body is read. A session flagged by signup gets it from the check. A session flagged by the Player create endpoint is signed in, so the check gives it allauth's 409 until it logs out, and the refusal after. Logout keeps the flag, as before.
- A missing or malformed value gets allauth's 400 with a field error, the same as signup's for that field. It does not set the flag.
- The check reads no email address. Other keys in the body are ignored, it looks up no Account or address, and it sends no mail, so its answer is the same for any address or none.
- It counts against allauth's `signup` rate limit, on the same counter as signup. The check and the signup together get no more attempts than signup alone, and an attacker cannot use the check to probe ages beyond that limit. A normal signup now costs two of the limit's requests (allauth's default is 20 a minute per IP).
- allauth's OpenAPI document lists the check. allauth builds the document from a fixed file with no hook to add a path, so OPEN serves `/_allauth/openapi.json` and `/_allauth/openapi.yaml` itself (`apps/accounts/openapi.py`): allauth's document, with the check added. The HTML page reads the JSON document, so it lists it too.

### Why this URL

The check is part of allauth's headless signup, not the OPEN API at `/api/v1/`:

- It must work for both headless clients before an Account exists, on the same session that signup then uses. allauth's headless views load the browser session or the app client's token session, and hand an app client a new token when the session changes. The OPEN API's `account_auth` accepts only a signed-in Account.
- The issue asks for allauth's error format: a 400 with field errors and signup's `signup_refused`. The OPEN API answers input errors with a 422 `ErrorResponse` (ADR 0005), and a different 400 shape there would break that rule.
- It shares allauth's signup rate limit and signup's 409 and 403 answers.

So it sits at allauth's signup path plus `/age-check`, under both clients' prefixes. It is a new path, served next to OPEN's versions of allauth's views (ADR 0014).

## Consequences

- The web and Expo apps call the check before showing the rest of screen 2, and still send the age with the signup request.
- The check and signup must keep the same rules. Both use `_screen_birth()` in `apps/accounts/headless_views.py`, and `SignupAgeForm` extends `SignupBirthForm`, so a rule changed in one place changes both. This answers ADR 0017's reasons for rejecting a separate age endpoint: the second request is the app's choice, and there is no second copy of the rules to keep in step.
- A refused session cannot pass the check by retrying. As ADR 0017 says, this stops a simple retry, not a determined one: a fresh session starts clean.
- OPEN's OpenAPI views call allauth's internal `get_schema()`. Check them when upgrading allauth; a test pins the listed path.
- The check is not in `openapi/openapi.json`, which documents only the OPEN API at `/api/v1/`.

## Alternatives considered

- **`POST /api/v1/signup/age-check` in the OPEN API.** It would need its own session and token handling for both clients, a second rate limit kept in step with allauth's, and a 400 format that ADR 0005 does not allow.
- **A separate rate limit for the check.** It would give a client more requests than signup alone allows, for no need of the app.
- **Holding the passed age for signup.** The issue asks that a pass hold nothing, and signup checks the age it is sent anyway. Holding it would add a second age in the session that could disagree with the signup request's.

# 0017. The signup age screen and the 13-and-older rule

- Status: Proposed
- Amended by: [ADR 0019](0019-gender-division-required-at-signup.md) (signup also takes the gender division, held with the age as `open_signup_details`)
- Date: 2026-11-17

## Context

ADR 0009 requires every player to be 13 or older, counted by the end-of-month rule. Signup must ask for the birth year and month first, on a neutral screen. Until the Player exists, the age may be held only in the signup session, on the server. ADR 0009 left the mechanics to issue #114.

Signup creates an Account (allauth's headless API, ADR 0014), but the Player comes later: the player verifies their email, logs in, and creates their Player (#124). So the age must outlive the signup request without being stored on the Account.

## Decision

### The age rules

- `apps/players/age.py` holds the rules. A player counts as born on the last day of their birth month. Born in February of a leap year, they count as born on the 29th, so in other years they turn a year older on 1 March.
- Every age-limit decision uses the earliest current date anywhere on Earth: the date at UTC-12 (`age_limit_date()`). A birthday counts only once it has begun everywhere. This leans toward the younger answer, as ADR 0009 asks.
- `Player` refuses a birth year and month under 13 whenever they are set or changed, with a fixed message that does not name the limit. So `create_player()`, and any later path that creates a Player, refuses it, and `update_profile()` refuses a change to an under-13 age. A stored Player is not re-checked when another field changes. A future month keeps its own message. The future-month check uses UTC, so for up to 12 hours at the start of a month, the new month gets the under-13 message instead. It is refused either way.
- `create_player()` sets every privacy setting to `PRIVATE` for a player aged 13 to 17. Nothing changes the settings at 18.

### The age screen at signup

- Signup on both headless clients takes two more required fields, `birth_year` and `birth_month` (`ACCOUNT_SIGNUP_FORM_CLASS`, so allauth's OpenAPI document lists them). ADR 0019 adds a third, `gender_division`, checked with them. The web and Expo apps ask for them on the first screen, before the email address.
- OPEN's signup view checks, in this order:
  1. allauth's signup rate limit;
  2. allauth's own answers for a signed-in session (409) and for closed signup (403);
  3. the age;
  4. allauth's signup input.
  The age is checked before allauth reads the email address, so a refused signup never looks up an address, creates nothing, and sends no mail.
- Under 13 gets one fixed 400 (`signup_refused`, "We can't create an account with these details."), whatever email and password come with it. The session then holds a flag, `open_signup_refused`, and no age. Every later signup in that session gets the same answer. Logout keeps the flag. ADR 0009 says a refused attempt keeps nothing. The flag is the one exception in the session: it holds no personal data, no age, and nothing about the email or password, and it lasts only as long as the session. Without it, the same session could simply retry with an older age. allauth's signup rate limit also counts refused requests, so its counter keeps the client's IP address in the cache for the limit's period, as for any other signup. An app client that sent no token gets one in the refusal, so its retries carry the flag too.
- A missing or malformed age gets allauth's 400 with a field error. It does not set the flag.
- A signup that passes holds `open_signup_details` in the session: the birth year, the month, the gender division (ADR 0019), and the new Account's id. #124's create endpoint reads it with `peek_signup_details(session, account)` and takes it with `take_signup_details(session, account)` (`apps/players/signup.py`) once the Player exists, or when the Account already has one. Both return it only to that Account, and the take removes it. The binding stops the age from reaching another Account that logs in later in the same browser. Login keeps it, and logout removes it.
- When the address is already some Account's login, allauth creates nothing, and the age is held bound to no Account. The session changes the same way as for a new address, so the app client's answer (a session token or not) does not reveal whether the address is taken. A repeated submit of the same new address keeps the earlier hold for the Account it created.

## Consequences

- The web and Expo apps must send the two fields with every signup.
- #124's create endpoint takes the age from the session. When the session no longer holds it (the player verified on another device, logged out, or the session expired), #124 must ask for the age again, with the same rule.
- The flag stops a simple retry, not a determined one. A browser that clears its cookies, or an app client that drops its token, starts a fresh session. So do the rare paths where Django flushes a session without a logout (a password change on another session, or a staff admin login to another Account). OPEN does not track people across sessions, because that would keep data from a refused attempt.
- A session refused once stays flagged even after a login to an existing Account in it, until the session ends.
- An age held for a taken address can never be taken. It stays in the session until the session ends.
- `SignupView` skips allauth's own decorated `handle` to count each signup once against the rate limit. Logic a future allauth version adds to that method would be skipped too. Check this when upgrading allauth.
- ADR 0009 gave the removal of an under-13 player's personal data to #114. It moves to #190, which builds on Account deletion (#116). Organizer CSV uploads carry the organizer's 13-and-older confirmation (#70, #71).

## Alternatives considered

- **A separate age endpoint before signup.** It matches "before email" more closely, but it adds a second request to every signup, and a second place that must keep the rate limit, the check order, and the session rules in step with allauth's signup.
- **Holding the age on the Account row.** ADR 0009 forbids an age on an Account with no Player.
- **Using the server's time zone (UTC) for age limits.** For up to 12 hours a day, a player in a time zone behind UTC would count a day older than they are where they live.

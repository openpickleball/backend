# 0043. Two-factor authentication with TOTP and recovery codes

- Status: Proposed
- Amended by: [ADR 0045](0045-harden-the-admin.md) (the admin login asks for the code through allauth's headless login, admin sessions are short, and writes need a recent code), [ADR 0046](0046-encrypt-personal-data-fields.md) (TOTP secrets and recovery code seeds are stored encrypted)
- Amended by: [ADR 0047](0047-account-security-limits-and-sessions.md) (removing TOTP ends the Account's other sessions)
- Amended by: [ADR 0056](0056-passkeys.md) (passkeys are a method and can sign in alone; every passkey change needs a recent reauthentication and is audited)
- Date: 2026-11-25
- Amended by: [ADR 0057](0057-organizer-and-club-admin-security.md) (removing TOTP is refused while it would leave an Organizer the Account is an admin of with no other active admin with MFA, so changing a method by removing TOTP first needs another such admin)

## Context

A password alone protects every Account (ADR 0014). Some Accounts can change other players' records and ratings: staff in the admin, moderators, members of a verified Organizer, whose matches count at once (ADR 0021), and owners of a Partner Application with production access. A stolen password for one of them is a security bug under hard rule 8. Issue #99 asks for two-factor authentication through allauth's headless API: TOTP and recovery codes, required for Organizer authority, staff, and moderators. Passkeys are #117.

## Decision

This amends ADR 0012 and ADR 0021 (a member's event authority also needs MFA), ADR 0014 (MFA is installed), and ADR 0029 (a moderator also needs MFA to reach the admin).

- **Methods.** `allauth.mfa` is installed with `MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]`. WebAuthn and passkey login and signup stay off until #117, and so does "trust this browser". Recovery codes are made with the first method and shown once (`MFA_RECOVERY_CODES_SHOW_ONCE`), and can be made again. Adding or removing TOTP, viewing the recovery codes, and making new ones need a recent reauthentication (`MFA_REAUTHENTICATION_REQUIRED`, allauth's 300 seconds), by password or MFA only (`ACCOUNT_REAUTHENTICATION_METHODS`): reauthentication by an emailed code is off, since its code would stand in for the second factor and allauth would mail its own template. A fresh login is not a recent reauthentication. OPEN's TOTP and recovery codes views ask for the reauthentication before they check a code, so a code is never tested for a session that may not act. The TOTP issuer is `OPEN`, so the request's host never names it. No method before the login address is verified. allauth's `mfa` extra is installed: `allauth.mfa` imports `fido2` at startup even with WebAuthn off.
- **What counts.** An Account has MFA when it has a TOTP authenticator (or WebAuthn, once #117 turns it on). Recovery codes alone do not count. `apps/accounts/mfa.py` holds the rules (`has_mfa`, `accounts_with_mfa`, `mfa_required`, `lacks_required_mfa`, `mfa_status`, read from the database by the Account's id, and `session_passed_mfa`, read from the session).
- **A session counts only once it passed MFA.** For an Account with MFA, only a session with an MFA step in allauth's login records counts (`session_passed_mfa`). A session that signed in with the password alone (the admin login until ADR 0045, or a session from before the Account enrolled) gets 403 `mfa_required` on the API and is refused by OPEN's MFA management views (adding and removing TOTP, viewing and making recovery codes) with allauth's 401 offering only `mfa_reauthenticate`, until it reauthenticates with a code. So, outside the admin, a stolen password never reaches Organizer authority, the API, or the Account's MFA settings. Enrolling records an MFA step for the enrolling session, so it keeps working. Since ADR 0045, a stolen password does not reach the admin either.
- **Organizer authority.** A member of a verified Organizer acts for it only while its Account has MFA. Every Account rule in `apps.matches.permissions` that grants Organizer authority reads membership through one function that checks it, and the queries that mirror those rules (the disputes and holds lists, the dispute and hold notice recipients) filter the same way. A player's self-submission needs no MFA. Applications are not affected.
- **The admin.** Every staff Account needs MFA. OPEN's admin site (`config/admin.py`) checks it on every request. Its login form refused a staff Account without one until ADR 0045, which replaced the form with a page that tells such an Account to set one up. The admin login asked for the password only; ADR 0045 (#101) makes it ask for the code.
- **The API.** An Account that must have MFA (staff, superusers, moderators, and owners of a non-sandbox Partner Application) and has none, and an Account with MFA whose session has not passed it, gets 403 with code `mfa_required` on every signed-in `/api/v1/` operation, from both clients. allauth's endpoints stay open, so it can sign in and enroll. The check runs after authentication and after the browser client's CSRF check. It costs one query per signed-in request.
- **Audit.** Each change writes one `mfa.change` Audit Event in the change's transaction, actor and target the Account: `added` or `removed` (TOTP or recovery codes), `regenerated` (recovery codes), and `used` (a recovery code, at login or reauthentication). It never holds a secret, a code, or a seed. allauth's signals run after commit, so OPEN serves subclasses of allauth's four MFA views at allauth's paths, each in `transaction.atomic()`. Each locks the Account row `FOR UPDATE` before any authenticator row, the order allauth's recovery code regeneration uses, so two removals cannot both record and a recovery code login cannot deadlock with a regeneration. The views also strip the Authenticator row id from allauth's `mfa` login records, so allauth's `methods` payload carries no row id (ADR 0014). Responses otherwise stay allauth's own, except for the session rule's 401 above and the reauthentication-first order (a session that has not reauthenticated gets 401 before its input is read).
- **Storage.** allauth stores TOTP secrets and the recovery code seed as it does, not encrypted. They are protected by access control: allauth's Authenticator admin is unregistered, and they are never logged or put in an Audit Event. ADR 0046 (#115) now encrypts them through OPEN's MFA adapter.
- **Email.** MFA sends no email: allauth's MFA notices go through its security notifications, which stay off (`ACCOUNT_EMAIL_NOTIFICATIONS`).

## Consequences

- A member, staff Account, moderator, or production application owner must set up TOTP before acting. Tests and dev data enroll them through the factories.
- An Account with MFA cannot add an email address (allauth's `add_email_blocked`, since verification by code is off). To change its address, a player removes TOTP, changes the address, and sets TOTP up again.
- A recovery code's use is locked by allauth (`FOR UPDATE` on the codes row) inside the view's transaction, so two logins with one code cannot both pass.
- A lost authenticator with no recovery codes locks the Account's authority until support recovers it, which is out of scope here.
- Turning on TOTP sends the Account's other password-only sessions to 403 `mfa_required` until each reauthenticates with a code or signs in again. The web app must handle that code.
- Removing TOTP ends the Account's other sessions (ADR 0047), except sessions with no user session row (made before ADR 0047). A session that passed MFA with a removed method still counts, which matters once the Account enrolls again; #232 keeps the rest of its scope.
- Until ADR 0045 (#101), the admin login asked for the password only, and such a session reached the admin of an enrolled staff Account, but not the API or the Account's MFA settings.
- Every Account that changed or used its MFA is the actor of `mfa.change` events, so it cannot be hard-deleted (ADR 0007). #116 plans for it.
- A superuser made with `createsuperuser` sets up TOTP through allauth's endpoints before the admin lets it in.

## Alternatives considered

- Recording the Audit Events from allauth's signals. Rejected: they run after commit, so a change could commit without its event.
- Requiring MFA for every Account. Rejected: a player who only reads and self-submits gains little, and it would be a barrier to joining.
- Encrypting the secrets now. Deferred to #115, which encrypts the other secret and identifying fields together.

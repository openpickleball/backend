# 0043. Two-factor authentication with TOTP and recovery codes

- Status: Proposed
- Date: 2026-11-25

## Context

A password alone protects every Account (ADR 0014). Some Accounts can change other players' records and ratings: staff in the admin, moderators, members of a verified Organizer, whose matches count at once (ADR 0021), and owners of a Partner Application with production access. A stolen password for one of them is a security bug under hard rule 8. Issue #99 asks for two-factor authentication through allauth's headless API: TOTP and recovery codes, required for Organizer authority, staff, and moderators. Passkeys are #117.

## Decision

- **Methods.** `allauth.mfa` is installed with `MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]`. WebAuthn and passkey login and signup stay off until #117, and so does "trust this browser". Recovery codes are made with the first method and shown once (`MFA_RECOVERY_CODES_SHOW_ONCE`), and can be made again. Adding or removing TOTP and making new codes need a recent reauthentication (`MFA_REAUTHENTICATION_REQUIRED`, allauth's 300 seconds). The TOTP issuer is `OPEN`, so the request's host never names it. No method before the login address is verified. allauth's `mfa` extra is installed: `allauth.mfa` imports `fido2` at startup even with WebAuthn off.
- **What counts.** An Account has MFA when it has a TOTP authenticator (or WebAuthn, once #117 turns it on). Recovery codes alone do not count. `apps/accounts/mfa.py` holds the rules (`has_mfa`, `accounts_with_mfa`, `mfa_required`, `lacks_required_mfa`, `session_passed_mfa`), each read from the database by the Account's id.
- **Organizer authority.** A member of a verified Organizer acts for it only while its Account has MFA. Every Account rule in `apps.matches.permissions` that grants Organizer authority reads membership through one function that checks it, and the queries that mirror those rules (the disputes and holds lists, the dispute and hold notice recipients) filter the same way. A player's self-submission needs no MFA. Applications are not affected.
- **The admin.** Every staff Account needs MFA. OPEN's admin site (`config/admin.py`) checks it on every request and its login form refuses a staff Account without one. The admin login asks for the password only; asking for the code there is #101.
- **The API.** An Account that must have MFA (staff, superusers, moderators, and owners of a non-sandbox Partner Application) and has none gets 403 with code `mfa_required` on every signed-in `/api/v1/` operation, from both clients. allauth's endpoints stay open, so it can sign in and enroll. The check runs after authentication and after the browser client's CSRF check. It costs one query per signed-in request.
- **Audit.** Each change writes one `mfa.change` Audit Event in the change's transaction, actor and target the Account: `added` or `removed` (TOTP or recovery codes), `regenerated` (recovery codes), and `used` (a recovery code, at login or reauthentication). It never holds a secret, a code, or a seed. allauth's signals run after commit, so OPEN serves subclasses of allauth's four MFA views at allauth's paths, each in `transaction.atomic()`; responses stay allauth's own.
- **Storage.** allauth stores TOTP secrets and the recovery code seed as it does, not encrypted. They are protected by access control: allauth's Authenticator admin is unregistered, and they are never logged or put in an Audit Event. #115 encrypts them.
- **Email.** MFA sends no email: allauth's MFA notices go through its security notifications, which stay off (`ACCOUNT_EMAIL_NOTIFICATIONS`).

## Consequences

- A member, staff Account, moderator, or production application owner must set up TOTP before acting. Tests and dev data enroll them through the factories.
- An Account with MFA cannot add an email address (allauth's `add_email_blocked`, since verification by code is off). To change its address, a player removes TOTP, changes the address, and sets TOTP up again.
- A recovery code's use is locked by allauth (`FOR UPDATE` on the codes row) inside the view's transaction, so two logins with one code cannot both pass.
- A lost authenticator with no recovery codes locks the Account's authority until support recovers it, which is out of scope here.
- `session_passed_mfa` is not used by any rule yet; #101, #117, and #232 use it.

## Alternatives considered

- Recording the Audit Events from allauth's signals. Rejected: they run after commit, so a change could commit without its event.
- Requiring MFA for every Account. Rejected: a player who only reads and self-submits gains little, and it would be a barrier to joining.
- Encrypting the secrets now. Deferred to #115, which encrypts the other secret and identifying fields together.

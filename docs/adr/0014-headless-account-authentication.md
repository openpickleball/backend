# 0014. Headless account authentication

- Status: Proposed
- Amended by: [ADR 0043](0043-mfa-totp-and-recovery-codes.md) (MFA with TOTP and recovery codes; a session of an Account with MFA counts only once it passed MFA)
- Date: 2026-11-17

## Context

OPEN's web app and Expo app need sign-up, login, email verification, and password reset through an API, not server-rendered pages (issue #30). ADR 0001 picked django-allauth for accounts. An Account's email is its login, and ADR 0003 makes one normalized ASCII address mean one Account. Finding an Account by email is the base for identity linking, a security-sensitive area (hard rule 8).

allauth keeps its own `EmailAddress` table next to `accounts.User.email`, and its flows find Accounts through that table, and through `User.email` when no verified row matches, with an exact match on a lowercased address. Review of #30 found that this lets an address that is not an Account's login reach the Account:

- An NFKC variant of an address, such as `ſam@example.org` for `sam@example.org`, passes Django's email validator. The manager folds it onto the Account, but allauth kept the raw value, stored it as a second address, and mailed it.
- A password reset matched a pending (unverified) change of address, or a verified row left behind when staff changed `User.email`, and mailed that Account's reset key there.
- Sign-up treated an address as taken when any row held it, so another Account's pending change of address, or a row left behind by an admin edit, kept the address's real owner from ever signing up.
- Verifying a change to an address another Account already held ended in a database error and left the two stores out of step.

## Decision

- django-allauth runs headless only, with both of its clients: `browser` (session cookie and CSRF) at `/_allauth/browser/v1/`, and `app` (`X-Session-Token`) at `/_allauth/app/v1/`. It is mounted at `/_allauth/`, allauth's default, so its documentation, OpenAPI document, and example frontends apply as written. Its OpenAPI document is served and linked from the OPEN API's description.
- Login is by email only. Email verification is mandatory. An Account has one login address: a change through allauth replaces it once verified. Any save that changes `User.email`, such as an admin edit, deletes the Account's rows for other addresses in the same transaction. No social login or user sessions app is installed. MFA came later, in [ADR 0043](0043-mfa-totp-and-recovery-codes.md).
- Verification and password reset use emailed links to pages of the web app at `FRONTEND_URL`, as the issue asks. `FRONTEND_URL` is required, an exact origin, and https when DEBUG is off.
- Sign-up and password reset answer the same way whether or not an Account has the address, and neither mails an address that has no Account.
- An address allauth stores (sign-up, change of address) is normalized and passes the Account email rules (`AccountAdapter.clean_email`). An address used to find an Account (login, password reset) must be canonical (`is_canonical_email`): stripped and lowercased, it equals its normalized form, and it is ASCII. Anything else finds no Account.
- Sign-up counts an address as taken only when it is some Account's login, so a pending change of address on another Account blocks nobody.
- Password reset finds an active Account only through the manager's natural-key lookup, and mails the key only to that Account's `User.email`. Verifying an address that is another Account's login is refused with allauth's `email_taken` error, and allauth's verify writes run in one transaction. OPEN serves its own versions of allauth's sign-up, reset request, and verify views, for both clients, ahead of allauth's. ADR 0020 adds OPEN's own signup age check under allauth's sign-up path, and OPEN's versions of allauth's OpenAPI JSON and YAML views, which add that path.
- `ModelBackend` stays first in `AUTHENTICATION_BACKENDS` for the Django admin, and it also authenticates every successful headless login, through the manager. OPEN's allauth backend comes second and also looks up through the manager.
- First-party API operations use `config.auth.account_auth`, which accepts either client. A failed check returns a 401 `ErrorResponse` with code `unauthorized`. The first such operation is `GET /api/v1/accounts/me`. ADR 0015 adds that a request with no signed-in user gets the 401 before any CSRF check, and that `request.auth` is the Account itself on both clients.
- allauth's user payload carries no row id. Its verification key (a signed `EmailAddress` id) and reset key (the Account id in base36) do encode one. They are accepted as an exception to the row-id rule, because only the mailbox owner sees them.
- The browser client works same-origin only: the web app is served from, or proxied through, the API host. `CORS_ALLOW_CREDENTIALS` stays False; ADR 0040 (#102) keeps it so while the browser client is same-origin. The app client works cross-origin: CORS allows the three headers allauth reads.

## Consequences

- iOS and Android apps use the app client. A web app can use the app client from any allowed origin, or the browser client same-origin. A session token kept in browser storage is exposed to any injected script, so the browser client is the safer choice for web, with the `__Host-` cookies and the Content Security Policy of ADR 0040.
- Email links open web pages. Opening them in the mobile apps needs Universal Links and App Links on the `FRONTEND_URL` domain, which is the apps' work.
- Until #35, a deployment with DEBUG off discarded mail, so nobody could verify an address or log in through allauth there. ADR 0016 adds a real provider (Resend) and sends account email from the email queue.
- An Account made with `createsuperuser` or the admin has no `EmailAddress` row. Its first headless login creates one, unverified, and asks for verification.
- The Django admin login does not check email verification, so a staff Account's admin session also authenticates `account_auth`. Staff Accounts are made by operators. Since ADR 0043 that session is password-only, so the API answers it with 403 `mfa_required`.
- A login typed with the Kelvin sign (U+212A) for a `k` still logs in. allauth's email field lowercases it to ASCII before OPEN's check, so it reaches the same Account that ADR 0003's normalization maps it to. It needs the password, stores nothing, and a reset with it mails only the stored address. This is accepted.
- allauth keys its per-address rate limits on the lowercased address as typed, so each refused variant has its own counter. Variants never log in, and the per-IP limits still apply. #100 sets the limits and the client IP behind the proxy.
- allauth sends at most one verification mail per address every three minutes. If allauth just mailed an address (for example for another Account's change of address), a sign-up with it in that window is created and answered as usual, but no verification mail is sent; the next login after the cooldown sends it. #100 sets the limits.
- Several Accounts can hold a row for one address: the Account whose login it is, if any, and any Accounts whose change of address to it is pending. Once the address is one Account's login, only that Account's row can be verified.
- Raw writes of `User.email` (`QuerySet.update`, `bulk_update`, raw SQL, `loaddata`) skip the row cleanup, so an Account's email changes only through a save.
- When allauth adds a view that finds an Account by email, it needs an override like the sign-up, reset, and verify ones, or it reopens the lookups above.
- allauth's endpoints use allauth's own response and error format, not the OPEN API's `ErrorResponse` (ADR 0005), so the web and Expo apps handle two error shapes.
- allauth's served OpenAPI document also describes endpoints and schemas OPEN does not enable, such as phone, code, and user session schemas.

## Alternatives considered

- **Emailed codes instead of links.** Codes carry no row id. The issue asks for links, and the keys are visible only to the mailbox owner.
- **Mount at `/api/auth/`.** It keeps CORS to `/api/` unchanged, but puts allauth's own versioned paths next to `/api/v1/` and departs from allauth's documentation.
- **Allow CORS credentials now.** That was #102's decision, which also has to keep embed origins (#96) from getting credentials. ADR 0040 keeps them off.
- **Use allauth's lookups unchanged.** They match pending and stale addresses, as described above.

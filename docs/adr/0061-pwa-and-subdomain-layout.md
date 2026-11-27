# 0061. A PWA for the MVP, and the openpb.org subdomain layout

- Status: Proposed
- Date: 2026-11-27

This sets the client and domain choices that [ADR 0014](0014-headless-account-authentication.md) left open. ADR 0014's decisions stand.

## Context

ADR 0014 and several issues assumed two first-party clients for the MVP: a web app and an Expo app. The plan is now one installable web app, a progressive web app (PWA), for the MVP, with native apps later (issue #182). OPEN's sites are also split across subdomains of `openpb.org`. Nothing recorded either choice, so issues still planned Expo work for v0.1, and the API's domain, cookie, CORS, and passkey settings had no agreed target.

The backend already supports both of allauth's clients (ADR 0014). The `browser` client uses the session cookie and CSRF, and works same-origin only, since `CORS_ALLOW_CREDENTIALS` stays False (ADR 0040). The `app` client sends `X-Session-Token` and works cross-origin. With DEBUG off, the session and CSRF cookies are `__Host-sessionid` and `__Host-csrftoken`, with no `Domain` (ADR 0040). Passkeys use the relying party ID `WEBAUTHN_RP_ID`, default `openpb.org`, and the exact origins in `WEBAUTHN_ALLOWED_ORIGINS`, default `[FRONTEND_URL]` (ADR 0056).

## Decision

- **One first-party client for the MVP.** The MVP's only first-party client is a PWA at `app.openpb.org`, built in the `openpickleball/frontend` repository. Native apps (Expo) come after the MVP. They will use the same API and allauth's `app` client.
- **Subdomain layout.**

  | Host | What it is | Source repository | What serves it |
  | --- | --- | --- | --- |
  | `openpb.org` (and `www.openpb.org`, which redirects to it) | The public site: teaser and about | `openpickleball/frontend` (`site/`) | A static site (GitHub Pages today; #304 plans App Platform) |
  | `app.openpb.org` | The PWA | `openpickleball/frontend` (the app), with this repository behind its API paths | App Platform's production app: the PWA's static build for every path, except `/api/`, `/_allauth/`, and `/oauth/`, which go to the Django component (#183, #304) |
  | `api.openpb.org` | The API for partners | This repository | App Platform's production app: the Django component (#183) |
  | `partner.openpb.org` | Partner integration docs and the published OpenAPI spec (Apache-2.0) | `openpickleball/partner-docs` (#184) | A static site (#184, #304) |
  | `dev.openpb.org` | A site about OPEN's tech stack and engine, for a technical audience | `openpickleball/developers` | A static site (GitHub Pages today; #304 plans App Platform) |
  | `status.openpb.org` | The public status page | `openpickleball/status` (#139) | GitHub Pages (Upptime, #139) |

  `app.openpb.org` and `api.openpb.org` are served by the same Django app, from the same image and settings. Which static host serves each site may change (#304) without changing this layout.
- **The PWA calls the API same-origin.** App Platform routes `app.openpb.org/api/`, `/_allauth/`, and `/oauth/` to the Django component, so the PWA's API calls go to its own origin. The PWA uses allauth's `browser` client (session cookie and CSRF), as ADR 0014 recommends for web. Partners use `api.openpb.org`. Why same-origin:
  - The browser client needs the session cookie on every API call. Cross-origin, that needs credentialed CORS (`CORS_ALLOW_CREDENTIALS`), which ADR 0040 keeps off; turning it on would also mean keeping embed origins (#96) from getting credentials.
  - The other way for a web app, the `app` client from another origin, keeps the session token in browser storage, where any injected script can read it. A session cookie is HttpOnly.
  - Same-origin calls need no CORS preflight, and `CORS_ALLOWED_ORIGINS` can stay empty for the PWA.
- **`FRONTEND_URL` is `https://app.openpb.org` in production**, so every link in OPEN's emails opens the PWA.
- **Cookies stay host-only.** `SESSION_COOKIE_DOMAIN` and `CSRF_COOKIE_DOMAIN` stay unset (None), and with DEBUG off both cookies keep the `__Host-` prefix (ADR 0040). The browser then keeps each cookie to the one host that set it, over https, and no other host can set a cookie of that name that reaches it. Other subdomains, some of them on GitHub Pages, must never read or plant OPEN's session or CSRF cookie. They are same-site with `app.openpb.org`, so `SameSite=Lax` alone would not stop them. A session on `app.openpb.org` is not a session on `api.openpb.org`, and nothing needs it to be.
- **Passkeys.** The WebAuthn relying party ID is `openpb.org`, the default of `WEBAUTHN_RP_ID`. It is the registrable domain, not the PWA's host, so passkeys keep working if the PWA moves to another subdomain, and a later native app can share them (#185). `WEBAUTHN_ALLOWED_ORIGINS` stays at its default, `[FRONTEND_URL]`, so in production passkeys work from `https://app.openpb.org` alone. OPEN refuses a credential whose origin is not exactly an allowed origin (ADR 0056), so a passkey assertion made on another subdomain never signs in. The system check `accounts.W001` warns when an allowed origin is not on the relying party ID or one of its subdomains; `https://app.openpb.org` is on `openpb.org`, so the production values pass it.

## Consequences

- There is one first-party client to build, test, and review, not two. The PWA needs no app store review, and contributors need only a browser.
- Email links and passkeys work in the PWA with no Universal Links, App Links, or associated domains files. Those wait for the native apps (ADR 0014's consequences, #185).
- iOS delivers web push to a PWA only after the player adds it to the Home Screen. Email (ADR 0016) is the main channel for match confirmation requests and match notices.
- iOS has no Background Sync. A match entered offline is sent when the PWA is next opened, not in the background. That is the PWA's work, in `openpickleball/frontend`.
- The Django component must answer on two hosts. `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, the App Platform routes, DNS, and HTTPS for both are #183's work, and whether the admin is reachable on either host is decided there. Nothing in this record changes a setting.
- Every other path on `app.openpb.org` goes to the PWA, so Django's other routes, such as `/.well-known/security.txt` and the admin, are not on that host unless #183 routes them there.
- The PWA's own paths must never start with `/api/`, `/_allauth/`, or `/oauth/`, since those go to Django on `app.openpb.org`.
- allauth's `app` client stays served and its CORS headers stay allowed (ADR 0014), so native apps can use it later with no change to the API. Native app passkeys need their origins allowed and the associated domains files served from `openpb.org`, which is #185.
- Expo work leaves v0.1. Issues that plan it move to a later milestone or have none.
- Moving the relying party ID off `openpb.org` later would strand every passkey made before the move. Keeping it on the registrable domain avoids that for any move between subdomains.

## Alternatives considered

- **Expo first.** One codebase for iOS, Android, and web. But it needs app store review for every release, Universal Links, App Links, and associated domains before email links and passkeys work, and a simulator or device for contributors. Its web build would still face the same browser choices as a PWA.
- **Both at once.** The plan ADR 0014 assumed. It doubles the client work and the review load for a volunteer project before the MVP ships, and the native app adds nothing the MVP needs that the PWA lacks.
- **The PWA on its own host, calling `api.openpb.org` cross-origin.** It needs credentialed CORS, which ADR 0040 keeps off, or the `app` client with a token in browser storage.
- **Domain-wide cookies (`Domain=openpb.org`).** One session across subdomains, but every subdomain, including the static sites, could then read or overwrite it.
- **The relying party ID `app.openpb.org`.** It narrows passkeys to the PWA's host, but any move of the PWA, and any native app on another host, would need every player to make new passkeys. The exact origin check already keeps other subdomains out.

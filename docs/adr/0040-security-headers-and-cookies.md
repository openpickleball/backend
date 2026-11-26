# 0040. Security headers, cookies, and a body limit

- Status: Proposed
- Date: 2026-11-25

## Context

ADR 0004 turned on https, secure cookies, and HSTS when DEBUG is off, and `manage.py check --deploy` passed. That check does not cover everything a public API host needs (issue #102). Nothing stopped an injected script from running, a page from loading a script from a CDN, or another site from framing a page. The session and CSRF cookies could be overwritten by a sibling subdomain. allauth's OpenAPI page loaded Redoc from a CDN. Django spools the files of a multipart body to disk before any view runs, and does not count them against its body limit, so any route could be made to write large uploads to disk. CI ran the deploy check only in a test, and a new deploy warning would not have failed it.

ADR 0014 left CORS credentials for the browser client to this issue. The web app is served from, or proxied through, the API host until the PWA work (#183), so the browser client is same-origin.

## Decision

- **Content Security Policy** with django-csp 4, on every response: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`. No `'unsafe-inline'`, no `'unsafe-eval'`, and no other host. `CSP_REPORT_ONLY` picks the mode: true sends only `Content-Security-Policy-Report-Only`, false only `Content-Security-Policy`. Unset or empty, it is report-only with DEBUG on (Django's debug pages use inline script and style) and enforced with DEBUG off. Any other value stops startup, naming the variable without the value. Production may set it true until the policy has been watched in real browsers (#68), then remove it. There is no report endpoint yet, so report-only violations reach only each browser's console.
- **allauth's OpenAPI page** (`/_allauth/openapi.html`) uses OPEN's template, `headless/spec/swagger.html` in `apps/accounts/templates/`. It loads django-ninja's bundled Swagger UI from the static files and keeps its settings in a JSON block, so it runs no inline script. `/api/v1/docs` already worked this way.
- **Standard headers** on every response: `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Cross-Origin-Opener-Policy: same-origin`, `X-Frame-Options: DENY`, and a `Permissions-Policy` that turns off accelerometer, autoplay, camera, display-capture, encrypted-media, geolocation, gyroscope, magnetometer, microphone, midi, payment, usb, and xr-spatial-tracking (`config/security_headers.py`). It leaves out `publickey-credentials-get` and `fullscreen`, so later passkeys and docs pages are not blocked, and it keeps a header a view already set. The CSP, Permissions-Policy, and X-Frame-Options middlewares come first in `MIDDLEWARE`, so responses that other middleware returns early (the 413 below, CORS preflights, static files, the https redirect) carry them too. HSTS is unchanged from ADR 0004: one year, `includeSubDomains`, `preload`.
- **No framing.** Every page refuses framing (`frame-ancestors 'none'` and `X-Frame-Options: DENY`). The embed (#96) adds its own exception for its views only.
- **Cookies.** With DEBUG off, the session cookie is `__Host-sessionid` and the CSRF cookie is `__Host-csrftoken`: Secure, path `/`, and no `Domain`, so a browser keeps each to this host over https. Both are `SameSite=Lax`. The session cookie is HttpOnly; the CSRF cookie is not, because allauth's browser client reads the token from it and sends it in `X-CSRFToken`. With DEBUG on, the names stay `sessionid` and `csrftoken`, so local http works.
- **CSRF trusted origins.** `CSRF_TRUSTED_ORIGINS` is checked like `CORS_ALLOWED_ORIGINS`: exact origins only, and https only when DEBUG is off. It is empty by default, and real hosts come from the environment.
- **CORS credentials stay off.** `CORS_ALLOW_CREDENTIALS` stays False. The browser client works same-origin; the app client works cross-origin with its session token.
- **A body limit on every route.** A declared `Content-Length` over `DATA_UPLOAD_MAX_MEMORY_SIZE` (Django's default, 2.5 MiB) gets a 413 `ErrorResponse` with code `too_large` before anything reads the body, on every path and method, signed in or not (`config/body_limits.py`). It runs right after `CorsMiddleware`, so the 413 carries CORS headers, and before the CSRF check. A POST to the CSV upload keeps its own limit and 413 (ADR 0036). A `Content-Length` that is not a whole number, or is negative, counts as 0, since Django then reads no body. The 413 is an `ErrorResponse` on every path, including `/_allauth/`, `/oauth/`, and the admin, which otherwise use their own error formats. The limit holds only under WSGI, as production serves the app (gunicorn and `config.wsgi`): Django's ASGI handler reads the whole body before any middleware runs, whatever `Content-Length` says.
- **CI fails on a deploy warning.** The `test` job runs `manage.py check --deploy --fail-level WARNING` with made-up production-like values. `SILENCED_SYSTEM_CHECKS` is empty, and any entry needs a comment saying why.

## Consequences

- A web app on the API host must read the CSRF token from `__Host-csrftoken` in production and `csrftoken` locally. The OpenAPI document's cookie auth names the session cookie that the serving settings use (django-ninja reads `SESSION_COOKIE_NAME`). The committed `openapi/openapi.json` is generated with `.env.example`'s DEBUG on, so it says `sessionid`, while a deployment with DEBUG off serves `__Host-sessionid`. The export and its `--check` must run with DEBUG on.
- Any new page or template must load its scripts and styles from the static files, with no inline script, style element, or style attribute, or the enforced policy blocks it. A page that needs an exception uses django-csp's per-view decorators, with a record of why.
- The 413 for an oversized body is not listed on each operation in the OpenAPI document.
- Serving the app over ASGI (`config/asgi.py`) would bypass the body limit, so it needs a body limit in the server first.
- A request body over 2.5 MiB is refused everywhere except the CSV upload. No route accepts one today; a later route that needs more raises the limit for its path only.
- Turning on credentialed CORS for first-party origins, a CSP report endpoint, and Django's built-in CSP (#69) are later decisions.

## Alternatives considered

- **Allow `'unsafe-inline'` for styles.** The admin and Swagger UI work without it, so it would only weaken the policy.
- **Enforce the policy at once, with no report-only switch.** A policy no real browser has run against the admin and docs could break them in production with no quick way back short of a deploy.
- **Plain cookie names with DEBUG off.** They work, but a sibling subdomain could set a cookie with the same name for the whole domain and fix a victim's session or CSRF token.
- **Allow CORS credentials for first-party origins now.** The browser client is same-origin until #183, and credentialed CORS would have to keep embed origins (#96) out. Not needed yet.
- **Limit bodies only at the proxy.** The proxy is outside this repository, its limit cannot tell the CSV upload from other routes, and a limit in the app is tested with the app.
- **Raise `DATA_UPLOAD_MAX_MEMORY_SIZE`, or keep Django's check alone.** Django's check skips multipart files, so it does not bound disk use.

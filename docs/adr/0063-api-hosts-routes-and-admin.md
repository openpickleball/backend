# 0063. The API's hosts, their routes, and where the admin answers

- Status: Proposed
- Date: 2026-11-27

This carries out the hosting part of [ADR 0061](0061-pwa-and-subdomain-layout.md) and makes the choice it left to issue #183: whether the admin is reachable on `app.openpb.org`, `api.openpb.org`, both, or neither. ADR 0061 also listed `CSRF_TRUSTED_ORIGINS` as #183's work; this record leaves it empty, for the reason below.

## Context

ADR 0061 puts the PWA on `app.openpb.org` and the partners' API on `api.openpb.org`, both served by one Django component in the production app (`.do/production.yaml`). On `app.openpb.org`, App Platform sends `/api/`, `/_allauth/`, and `/oauth/` to Django, and every other path to the PWA's static site (#304). The spec still set `DJANGO_ALLOWED_HOSTS` to `${APP_DOMAIN}`. App Platform fills that binding with the app's primary domain, so with a custom domain it names one host, never both.

The admin is served at the private path `ADMIN_URL` (ADR 0045). Its login page signs in through allauth's browser client at `/_allauth/` on the same origin, and it loads its scripts and styles from `/static/`.

App Platform's docs do not say in what order it tries ingress rules, or what it answers for a request no rule matches.

## Decision

- **Domains.** The production app has two custom domains: `api.openpb.org` (primary) and `app.openpb.org` (alias), each with TLS 1.2 or later, and no wildcard. DNS for `openpb.org` is not on DigitalOcean, so the spec sets no `zone`: each host is a CNAME to the app's `ondigitalocean.app` host, set at the DNS provider, and not proxied by the DNS provider, so App Platform issues the certificates and the client IP that `do-connecting-ip` carries is the player's, not a proxy's (ADR 0030).
- **Routes.** On `app.openpb.org`, `/api/`, `/_allauth/`, and `/oauth/` go to `web`. On `api.openpb.org`, every path goes to `web`. Every rule names its exact host and keeps its path prefix (`preserve_path_prefix`), so no rule depends on the order App Platform tries them in, and Django sees the same path on either host. The PWA's static site and its catch-all rule on `app.openpb.org` are #304's.
- **Hosts.** `DJANGO_ALLOWED_HOSTS` is `api.openpb.org,app.openpb.org`, written out, not `${APP_DOMAIN}`. The app's `ondigitalocean.app` host is left out, so the API and admin have one address per audience. No ingress rule names that host either, so App Platform should not route it to Django, and if it does, Django answers 400. App Platform's health probes still pass: they go straight to the container, and `HealthProbeMiddleware` answers them whatever the host (ADR 0042).
- **No trusted origins.** `CSRF_TRUSTED_ORIGINS` stays unset (empty). The PWA's requests and the admin's are same-origin, and Django's origin check already accepts an `Origin` that matches the request's own host over https. Django trusts a listed origin on every host. Listing `https://app.openpb.org` would let a request from the PWA's host pass the origin check on `api.openpb.org`, where the admin is. The two hosts are same-site, so `SameSite=Lax` sends the admin's session cookie on such a request. With the list empty, the origin check refuses it. The CSRF token from the host's own `__Host-csrftoken` cookie, which no other host can read or plant (ADR 0040), is then a second check, not the only one.
- **The admin answers on `api.openpb.org` only.** It is not routed on `app.openpb.org`: settings already refuse an `ADMIN_URL` that starts with `api/`, `_allauth/`, `oauth/`, or `static/`, so it can never fall under a path routed to Django there. A test keeps the routed paths within that refused set.

## Consequences

- The admin's session cookie lives on `api.openpb.org`, apart from the PWA's session on `app.openpb.org`. A flaw in the PWA's scripts runs on `app.openpb.org`, where no admin page exists. A request it sends to `api.openpb.org` carries the admin's session cookie (the hosts are same-site), but fails the origin check and has no CSRF token for that host.
- Staff sign in to the admin on `api.openpb.org`, where `/_allauth/` and `/static/` both reach Django, so its login page and assets work as they do locally.
- Django's pages that load static files (`/api/v1/docs` and `/_allauth/openapi.html`) work on `api.openpb.org` only. On `app.openpb.org` their `/static/` paths go to the PWA. The partners' docs are on `api.openpb.org` and, later, `partner.openpb.org` (#184).
- `/.well-known/security.txt` is Django's on `api.openpb.org`. On `app.openpb.org` it is the PWA's path (ADR 0061).
- Every other path on `app.openpb.org` goes to the PWA's static site, `app`, through a catch-all rule listed after the three Django rules on that host (#304). On each host no rule comes after a shorter prefix of it, so first match and longest prefix give the same result.
- What the `ondigitalocean.app` host answers through App Platform's edge is not documented; no rule routes it, so it should not reach Django. Checks during the first deploy (#62) use the custom domains, once DNS is set. Confirm then that a path such as `/api-x/` on `app.openpb.org` does not reach Django, since the docs do not say whether a prefix ending in `/` matches by path segment.
- Adding a host means changing `domains`, the rules, and `DJANGO_ALLOWED_HOSTS` together; `config/tests/test_app_spec.py` keeps them in step. A cross-origin first-party client needs CORS and a new decision, not a trusted origin.

## Alternatives considered

- **`DJANGO_ALLOWED_HOSTS` from `${APP_DOMAIN}`.** It names only the primary domain, so Django would refuse the other host.
- **Keeping the `ondigitalocean.app` host allowed.** A second public address for the API and admin, with nothing that needs it.
- **The admin on `app.openpb.org` too, or only.** It would need a route for its private path on the PWA's host, which a public spec would reveal, and a route for `/static/`. The admin's session would then share a host, and its cookie, with the PWA's scripts.
- **No admin on either host.** Staff need it to verify Organizers, void matches, and close reports (ADR 0045), and there is no other way in.
- **`CSRF_TRUSTED_ORIGINS` listing both hosts.** Issue #183 first planned it. Nothing needs it, and it would make each host trust the other's origin, leaving the CSRF token as the only check between the PWA's host and the admin.

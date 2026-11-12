# 0004. Settings from the environment, secure by default

- Status: Proposed
- Date: 2026-11-11

## Context

The same image must run locally, in CI, in staging, and in production, and no secret may live in the repository (issue #4). Settings therefore come from environment variables, read with django-environ (decided in issue #1). Production will run on DigitalOcean App Platform, behind a proxy that terminates TLS and sets `X-Forwarded-Proto`.

Some of these choices are hard to undo. A browser that has seen an HSTS header keeps forcing https for the whole max-age, and a default that is unsafe in production stays unsafe until someone notices.

## Decision

- `DJANGO_DEBUG` is off unless set to a true value. With it off, a missing or blank `DJANGO_SECRET_KEY` stops startup. With it on and no key set, a fixed development-only key (prefixed `django-insecure-`) is used.
- `DATABASE_URL` and `REDIS_URL` are required. A malformed `DATABASE_URL` or `REDIS_URL` stops startup with an error that names the variable and does not echo the value.
- Values are read literally. django-environ's getters are not used, because they treat a value starting with `$` as a reference to another variable, and a generated secret key can start with `$`.
- A `.env` file at the repository root is read only if it exists, and the process environment wins over it. `.env.example` holds placeholders only.
- With DEBUG off: `SECURE_PROXY_SSL_HEADER` trusts `X-Forwarded-Proto`, `SECURE_SSL_REDIRECT` is on, session and CSRF cookies are secure, and HSTS is sent for one year with `includeSubDomains` and `preload`. `manage.py check --deploy` reports no issues with production-like values.

## Consequences

- Every command that loads settings, `manage.py check` included, needs a `.env` or the variables in the environment.
- Any host served with DEBUG off must stay https-only, subdomains included, for a year after a browser last saw the header. The `preload` directive submits nothing by itself; joining a browser preload list is a separate, manual step.
- Trusting `X-Forwarded-Proto` is safe only behind a proxy that sets it. Running the app with DEBUG off without such a proxy lets a client claim https.
- A plain-http health probe gets a redirect. The health check issue (#66) needs to exempt its path or probe over https.
- Tests run over plain http, so the test runner turns the redirect off for the test run only (`config/test_runner.py`). It also swaps the cache to an in-memory one, so tests need no cache server. Any replacement runner must do the same.

## Alternatives considered

- DEBUG on by default: convenient locally, but a forgotten variable in production would expose debug pages and the development key.
- A short, configurable HSTS max-age to start: Django's checklist suggests ramping up. Rejected for a fixed year, so production cannot run with a weaker value by mistake.
- Optional `DATABASE_URL` and `REDIS_URL` with localhost defaults: a missing variable in production would point the app at localhost instead of failing at startup.

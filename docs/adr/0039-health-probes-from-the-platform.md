# 0039. Health probes from the platform

- Status: Proposed
- Date: 2026-11-25

## Context

DigitalOcean App Platform checks the `web` component with HTTP health probes (issue #61): a readiness check that stops traffic and a liveness check that restarts the container. The probes go straight to the container, not through the platform's edge. They come over plain http, and their Host header is empty or the instance's own IP address, which `ALLOWED_HOSTS` refuses. App Platform offers no way to set the probe's Host header.

With `DJANGO_DEBUG` off, `SECURE_SSL_REDIRECT` is on, and `SecurityMiddleware` answers plain http first. To build its redirect it asks for the host, so a refused host raises `DisallowedHost`, which Django answers with 400. An allowed host over plain http gets a redirect to https, which the probe reads as a failure too. Either way the readiness check fails and a deploy never goes live. Django's `DisallowedHost` log line also quotes the refused host on every probe.

## Decision

- A first middleware, `HealthProbeMiddleware` (`config/health_probe.py`), answers the platform's probes. It acts only on a GET whose `path_info` is exactly `/api/v1/health/live` or `/api/v1/health/ready`, and only when the host is not allowed or when `SECURE_SSL_REDIRECT` is on and the request is not secure. Then it calls the resolved health view itself and returns its answer, with no other middleware.
- Every other request goes down the normal chain unchanged: another path, another method, a near-miss path (a trailing slash, an extra segment), and a health GET with an allowed host over https. A bad host on any other path still gets 400, and plain http on any other path is still redirected.
- The two paths are constants. A test checks that they resolve to the views in `config/health.py`.
- The middleware never logs the host, and never raises `DisallowedHost` on its path, so Django's log line does not run either.

## Consequences

- This amends ADR 0004's consequence that a plain-http health probe gets a redirect and that the health check issue must exempt its path. The two health paths answer a plain-http probe directly. Every other path is still redirected.
- The health checks answer anyone who reaches them, over http and with any Host header. They take no auth and no input, read no session or cookie, and say only "ok" or "fail" per check, never why, so this exposes nothing new. They were already public.
- A probe answer carries no security headers (HSTS, `X-Frame-Options`, CORS) and sets no cookie. A health GET with an allowed host over https gets them as before.
- The readiness check still touches the database and the cache on every probe, as before.
- A new health path, or a change to the health mount, needs the constants changed too. The test fails until they are.

## Alternatives considered

- Adding the instance IP to `ALLOWED_HOSTS`: the address is not known before the container starts and changes with every instance, and an empty Host could never match.
- Allowing every host (`ALLOWED_HOSTS = ["*"]`): drops Django's Host header protection for the whole app.
- `SECURE_REDIRECT_EXEMPT` for the health paths: fixes the redirect but not the 400 for the host.
- TCP health checks: they prove only that the port is open, so a database or cache outage would never stop traffic.
- Answering the probes from a separate process on another port: that process could pass while gunicorn fails.

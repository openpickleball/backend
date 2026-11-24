# 0030. API throttling per Account and per client IP

- Status: Proposed
- Date: 2026-11-24

## Context

Issue #34: one client must not be able to overload the service. First-party operations need sign-in, so they can be limited per Account. The public ratings endpoint (#53) will need no sign-in, so its only key is the client IP.

ninja's built-in throttles do not fit. `AuthRateThrottle` keys on a hash of `str(request.auth)`, the Account's email. Their read and write of a count is not atomic, so parallel requests can exceed a limit. ninja asks every throttle on an operation, so a request one throttle refuses still counts against the others. ADR 0018 built player search's own throttle to avoid these problems.

The app runs on DigitalOcean App Platform. Its proxy puts the ingress address in `X-Forwarded-For` and sets `do-connecting-ip` to the client IP. A client can send any `X-Forwarded-For` value.

## Decision

- Throttles are fixed-window counters in `config/throttling.py`, built like ADR 0018's: `cache.add` then `cache.incr` in the default cache (Valkey), a minute window and a day window, keys of scope, window, bucket, and window index. A request the minute window refuses does not count toward the day. Nothing about a request is kept on the shared throttle object.
- Every first-party operation that needs sign-in shares one per-Account throttle, keyed by the Account's id, never its email. Each router passes it next to its `auth`. A request refused by `auth` (401) is not counted.
- Player search keeps only its own throttle and fixed limits (ADR 0018). ninja lets an operation's own throttle replace its router's, and a test pins that.
- The health checks are never throttled, so a platform probe is never refused.
- A public operation uses the client IP throttle. The IP comes only from the header named by the `CLIENT_IP_HEADER` setting, default `do-connecting-ip`. `X-Forwarded-For`, `Forwarded`, and `REMOTE_ADDR` are never read, and settings refuse `X-Forwarded-For` and `Forwarded` as the header (and `Content-Type` and `Content-Length`, which never reach Django as `HTTP_` headers). A missing or invalid value, a comma list included, falls into one shared bucket. An IPv4-mapped IPv6 address counts as its IPv4 address. Other IPv6 addresses count by their /64, so a client cannot escape the limit by changing addresses inside its /64.
- Rates come from settings, read on each request: `API_ACCOUNT_THROTTLE_PER_MINUTE` (default 120), `API_ACCOUNT_THROTTLE_PER_DAY` (10,000), `API_CLIENT_IP_THROTTLE_PER_MINUTE` (60), and `API_CLIENT_IP_THROTTLE_PER_DAY` (3,000). Each is a whole number from 1 to 1,000,000.
- Past a limit the answer is 429 with code `throttled`, no items, and a `Retry-After` header with the whole seconds until the refusing window ends, or until a later window ends when that window is already full, at least 1. CORS exposes `Retry-After`, so a browser client on an allowed origin can read it. The OpenAPI document shows this 429 on every throttled operation.

## Unverified header trust

Whether App Platform replaces or strips a `do-connecting-ip` header that the client itself sends is not verified. If it passes a client's value through, a client can choose its own bucket and escape the per-IP limit. A deploy-time check on #61 must show that the proxy overwrites the header. #53, the public ratings endpoint, must not go public before that check passes.

## Consequences

- One budget covers all of an Account's first-party requests except search. A client app that polls one operation uses up the budget for the others.
- Fixed windows allow up to about twice a limit within seconds across a window edge. Accepted, as in ADR 0018.
- If the client IP header is missing in production, every public request shares the fallback bucket, so one busy client can block the rest. That is an outage, not a way around the limit.
- The counters live in Valkey. If Valkey is down, every throttled operation fails, which is every first-party operation that needs sign-in, not only search. The health checks still answer. Clearing the cache resets the counters.
- The client IP, with IPv6 cut to its /64, is held in cache keys. A key expires 60 seconds after its window ends (personal data inventory). Every key gets an expiry, even when Valkey evicts it between the existence check and the increment.
- A client that holds more than one /64 (a /56 or /48 delegation, or a 6to4 address) gets one bucket per /64. Revisit the prefix length, or add a coarser window, before #53 goes public (ADR 0031).
- Per-application limits for partner operations are #119. Edge or CDN rate limits are out of scope.

## Alternatives considered

- **ninja's built-in throttles.** Rejected for the reasons in Context.
- **A per-Account throttle on search as well.** ninja would ask both throttles, and each would count requests the other refuses (ADR 0018).
- **Take the client IP from `X-Forwarded-For`.** On App Platform its last entry is the ingress, and a client can put any value before it.
- **Refuse a request with no valid client IP.** Safer against a missing header, but a proxy change would turn every public request into an error. The owner chose the shared bucket.

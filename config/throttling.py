"""Fixed-window throttles for the OPEN API (issue #34, ADR 0030).

ninja's built-in throttles are not used: ``AuthRateThrottle`` keys on a hash
of ``str(request.auth)``, the Account's email, and their read and write of
a count is not atomic. These throttles count instead with ``cache.add``
then ``cache.incr`` in the default cache, which are atomic on the Redis and
local memory caches, so limits hold across web instances and workers.

Each request is counted against a list of windows (a minute and a day).
Each window is a fixed window: one counter per scope, window name, bucket,
and window index (``int(now // period)``), kept for the window plus a
margin. Windows are checked in order, and a later window counts the
request only when every earlier one allowed it, so a burst the minute
limit refuses does not use up the day. Fixed windows allow a burst of up
to twice a limit across a window edge. Accepted.

- ``AccountThrottle`` keys by the signed-in Account's primary key, never
  its email. A request with no Account is not counted (ninja checks auth
  first, so a 401 never reaches a throttle). ``account_throttle`` is the
  one instance every first-party router shares.
- ``ClientIPThrottle`` keys by the client IP from the request header named
  by the ``CLIENT_IP_HEADER`` setting, and nothing else: never
  ``X-Forwarded-For``, ``Forwarded``, or ``REMOTE_ADDR`` (``REMOTE_ADDR``
  is the proxy). A missing or invalid value, a comma list included, falls
  into one shared bucket. An IPv4-mapped IPv6 address counts as its IPv4
  address, and other IPv6 addresses count by their /64 prefix, so a client
  cannot rotate addresses within its /64 to escape the limit. Whether
  App Platform replaces a ``do-connecting-ip`` header the client sent is
  unverified until the deploy-time check on #61 passes; until then no
  public operation goes live (#53).

Rates are read from settings on every request, so ``override_settings``
and a changed environment take effect at once.

ninja builds one throttle list per operation and shares it across requests
and threads, so nothing about a request is stored on ``self``. What
``wait()`` needs (when the refusing window ends) is kept in a
``threading.local``: ninja calls ``wait()``, with no request, right after
``allow_request`` returns False, on the same thread. ninja asks every
throttle on an operation, so an operation has exactly one throttle.
"""

import ipaddress
import threading
import time
from collections.abc import Callable

from django.conf import settings
from django.core.cache import cache as default_cache
from django.http import HttpRequest
from ninja.throttling import BaseThrottle

MINUTE = 60
DAY = 24 * 60 * 60
# Extra seconds a counter outlives its window, so it is never dropped
# while its window is still open.
THROTTLE_TIMEOUT_MARGIN = 60
# The one bucket for every request without a valid client IP.
FALLBACK_BUCKET = "unknown"
_IPV6_PREFIX = 64

Windows = tuple[tuple[str, int, int], ...]


class FixedWindowThrottle(BaseThrottle):
    """A throttle that counts each request in fixed windows.

    A subclass gives ``bucket``, what a request is counted under (None
    to not count it), and ``limits``, the (name, limit, period in seconds)
    windows, checked in order.
    """

    cache = default_cache
    # A plain function, so tests can replace the clock.
    timer: Callable[[], float] = staticmethod(time.time)

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self._refused = threading.local()

    def bucket(self, request: HttpRequest) -> str | None:
        raise NotImplementedError

    def limits(self) -> Windows:
        raise NotImplementedError

    def _count(self, key: str, period: int) -> int:
        """Add one to a window's counter and return the new count.

        If the counter expires or is evicted between ``add`` and ``incr``
        (``incr`` raises ``ValueError``), try once more, then allow the
        request (count 0) rather than fail it.
        """
        timeout = period + THROTTLE_TIMEOUT_MARGIN
        for _ in range(2):
            self.cache.add(key, 0, timeout)
            try:
                return int(self.cache.incr(key))
            except ValueError:
                continue
        return 0

    def allow_request(self, request: HttpRequest) -> bool:
        self._refused.retry_after = None
        bucket = self.bucket(request)
        if bucket is None:
            return True
        now = self.timer()
        for name, limit, period in self.limits():
            window = int(now // period)
            key = f"throttle:{self.scope}:{name}:{bucket}:{window}"
            if self._count(key, period) > limit:
                self._refused.retry_after = max(1.0, (window + 1) * period - now)
                return False
        return True

    def wait(self) -> float | None:
        retry_after: float | None = getattr(self._refused, "retry_after", None)
        return retry_after


def account_bucket(request: HttpRequest) -> str | None:
    """The signed-in Account's primary key, or None when there is none."""
    account_pk = getattr(getattr(request, "auth", None), "pk", None)
    return None if account_pk is None else str(account_pk)


class AccountThrottle(FixedWindowThrottle):
    """Per-Account limits from ``API_ACCOUNT_THROTTLE_PER_MINUTE`` and
    ``API_ACCOUNT_THROTTLE_PER_DAY``."""

    def bucket(self, request: HttpRequest) -> str | None:
        return account_bucket(request)

    def limits(self) -> Windows:
        return (
            ("minute", settings.API_ACCOUNT_THROTTLE_PER_MINUTE, MINUTE),
            ("day", settings.API_ACCOUNT_THROTTLE_PER_DAY, DAY),
        )


def client_ip_bucket(request: HttpRequest) -> str:
    """The client IP bucket for a request, from ``CLIENT_IP_HEADER`` only."""
    meta_key = "HTTP_" + settings.CLIENT_IP_HEADER.upper().replace("-", "_")
    value = request.META.get(meta_key)
    if not isinstance(value, str):
        return FALLBACK_BUCKET
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return FALLBACK_BUCKET
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        prefix = int(address) >> (128 - _IPV6_PREFIX) << (128 - _IPV6_PREFIX)
        return str(ipaddress.IPv6Network((prefix, _IPV6_PREFIX)))
    return str(address)


class ClientIPThrottle(FixedWindowThrottle):
    """Per client IP limits from ``API_CLIENT_IP_THROTTLE_PER_MINUTE`` and
    ``API_CLIENT_IP_THROTTLE_PER_DAY``, for public operations."""

    def bucket(self, request: HttpRequest) -> str | None:
        return client_ip_bucket(request)

    def limits(self) -> Windows:
        return (
            ("minute", settings.API_CLIENT_IP_THROTTLE_PER_MINUTE, MINUTE),
            ("day", settings.API_CLIENT_IP_THROTTLE_PER_DAY, DAY),
        )


# The throttle every first-party router that needs sign-in shares.
account_throttle = AccountThrottle("account")

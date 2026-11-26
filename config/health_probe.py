"""Answer the platform's health probes ahead of Django's host check.

DigitalOcean App Platform sends its HTTP health probes straight to the
container, over plain http, with a Host header that ALLOWED_HOSTS refuses
(empty, or the instance IP). With SECURE_SSL_REDIRECT on,
SecurityMiddleware answers plain http first: building its redirect asks
for the host, so a refused host raises DisallowedHost (400), and an
allowed host gets a redirect to https. Either way the readiness check
would fail and a deploy never go live.
The platform offers no way to set the probe's Host header. ADR 0042
records the decision.

This middleware comes right after the header middlewares (ADR 0040)
and ahead of SecurityMiddleware. For a GET of exactly one of PROBE_PATHS
(``request.path_info``, no trailing slash or other variant), it calls the
health view itself, with no other middleware, when the host is not
allowed, or when SECURE_SSL_REDIRECT is on and the request is not secure.
Every other request goes down the normal chain unchanged, including a
health GET with an allowed host over https, so a bad host on any other
path still gets 400.

The health views take no auth, read no session, cookie, or input, and
answer only "ok" or "fail" per check, so skipping the other middleware
exposes nothing. A probe answer sets no cookie and gets no HSTS or CORS
headers; the header middlewares ahead of it still add the Content
Security Policy, Permissions-Policy, and X-Frame-Options.
The refused host is never logged: Django's own DisallowedHost log line
quotes it, and this path never raises it.
"""

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import HttpRequest, HttpResponseBase
from django.urls import resolve

# The liveness and readiness checks in config/health.py, as mounted in
# config/api.py. A test checks that they resolve to those views.
PROBE_PATHS = frozenset({"/api/v1/health/live", "/api/v1/health/ready"})


def _needs_direct_answer(request: HttpRequest) -> bool:
    """Whether the normal chain would refuse or redirect this probe."""
    try:
        request.get_host()
    except DisallowedHost:
        return True
    return bool(settings.SECURE_SSL_REDIRECT) and not request.is_secure()


class HealthProbeMiddleware:
    """Serve the two health checks to the platform's probes directly."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if (
            request.method == "GET"
            and request.path_info in PROBE_PATHS
            and _needs_direct_answer(request)
        ):
            match = resolve(request.path_info)
            response: HttpResponseBase = match.func(
                request, *match.args, **match.kwargs
            )
            return response
        return self.get_response(request)

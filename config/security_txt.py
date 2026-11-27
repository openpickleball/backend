"""The security.txt file (RFC 9116, issue #105), at /.well-known/security.txt.

It tells security researchers how to report a problem with the hosted
service. Every value comes from settings (the security.txt block in
``config/settings.py``): ``Contact`` and ``Canonical`` from the
environment, so each environment sets its own, and ``Policy`` and
``Preferred-Languages`` fixed there. ``Expires`` is worked out on each
request, ``SECURITY_TXT_EXPIRES_DAYS`` after the start of the current UTC
day, so the file never goes stale while the service runs.

With no ``SECURITY_TXT_CANONICAL``, ``Canonical`` is this request's own
URL. The host in it has passed ``ALLOWED_HOSTS``, and the scheme is https
in production, where the https redirect comes first.

The response is plain text, takes no auth, and is not part of the API, so
it is not in the OpenAPI document.
"""

from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_safe


def _expires(now: datetime) -> str:
    """Return the Expires value: the start of today, UTC, plus the days."""
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (day + timedelta(days=settings.SECURITY_TXT_EXPIRES_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def security_txt_content(request: HttpRequest, now: datetime) -> str:
    """Build the file's text for ``request`` at the time ``now``."""
    canonical = settings.SECURITY_TXT_CANONICAL or request.build_absolute_uri(
        settings.SECURITY_TXT_PATH
    )
    lines = [
        f"Contact: {settings.SECURITY_TXT_CONTACT}",
        f"Expires: {_expires(now)}",
        f"Policy: {settings.SECURITY_TXT_POLICY}",
        f"Preferred-Languages: {settings.SECURITY_TXT_PREFERRED_LANGUAGES}",
        f"Canonical: {canonical}",
    ]
    return "\n".join(lines) + "\n"


@require_safe
def security_txt(request: HttpRequest) -> HttpResponse:
    """Serve /.well-known/security.txt (GET and HEAD only)."""
    return HttpResponse(
        security_txt_content(request, datetime.now(UTC)),
        content_type="text/plain; charset=utf-8",
    )

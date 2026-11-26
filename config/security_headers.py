"""The Permissions-Policy header on every response (issue #102, ADR 0040).

Django sets the other security headers itself (SecurityMiddleware and
XFrameOptionsMiddleware), and django-csp sets the Content Security
Policy. Django has no setting for Permissions-Policy, so this middleware
turns off every browser feature in ``DISABLED_FEATURES``, which no OPEN
page uses. A header a view already set is kept.

``publickey-credentials-get`` (passkeys, for a later MFA issue) and
``fullscreen`` are left out on purpose, so those can work when needed.
"""

from collections.abc import Callable
from typing import Final

from django.http import HttpRequest, HttpResponseBase

DISABLED_FEATURES: Final = (
    "accelerometer",
    "autoplay",
    "camera",
    "display-capture",
    "encrypted-media",
    "geolocation",
    "gyroscope",
    "magnetometer",
    "microphone",
    "midi",
    "payment",
    "usb",
    "xr-spatial-tracking",
)

HEADER: Final = "Permissions-Policy"
POLICY: Final = ", ".join(f"{feature}=()" for feature in DISABLED_FEATURES)


class PermissionsPolicyMiddleware:
    """Adds ``Permissions-Policy`` unless the response already has one."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        if HEADER not in response:
            response[HEADER] = POLICY
        return response

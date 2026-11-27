"""System checks for the accounts app (issue #117, ADR 0056).

``accounts.W001``: every WebAuthn allowed origin must be on the relying
party ID or one of its subdomains, or browsers refuse to use a passkey
there. A warning, not a startup error, so a local ``.env`` from before
passkeys keeps working; CI's deploy check fails on it. The message names
the settings, never the origin.
"""

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from django.apps import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Warning


def origin_under_rp_id(origin: str, rp_id: str) -> bool:
    """Whether ``origin``'s host is ``rp_id`` or a subdomain of it."""
    host = urlsplit(origin).hostname or ""
    return host == rp_id or host.endswith(f".{rp_id}")


def check_webauthn_origins(
    app_configs: Sequence[AppConfig] | None, **kwargs: Any
) -> list[CheckMessage]:
    rp_id: str = settings.WEBAUTHN_RP_ID
    origins: list[str] = settings.WEBAUTHN_ALLOWED_ORIGINS
    if all(origin_under_rp_id(origin, rp_id) for origin in origins):
        return []
    return [
        Warning(
            "An entry of WEBAUTHN_ALLOWED_ORIGINS is not on WEBAUTHN_RP_ID or "
            "one of its subdomains, so passkeys cannot be used from it.",
            hint=(
                "Set WEBAUTHN_RP_ID to the registrable domain the origins "
                "share, or leave WEBAUTHN_ALLOWED_ORIGINS empty to use "
                "FRONTEND_URL (see .env.example)."
            ),
            id="accounts.W001",
        )
    ]

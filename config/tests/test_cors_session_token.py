"""A browser-hosted app client may send X-Session-Token cross-origin
(issue #30).

allauth's app client, run in a browser on an allowed origin (such as the
Expo web build), sends its session token in the X-Session-Token header,
so the CORS preflight must allow that header. Credentials stay off.
"""

from collections.abc import Iterator

import pytest
from corsheaders.defaults import default_headers
from django.conf import settings
from django.test import Client, override_settings

ALLOWED = "https://app.example.org"


@pytest.fixture
def allowed_client() -> Iterator[Client]:
    with override_settings(CORS_ALLOWED_ORIGINS=[ALLOWED]):
        yield Client()


def test_allowed_headers_are_the_defaults_plus_allauths_headers() -> None:
    # Plus the app client's device token, sent with a login (#100).
    assert tuple(settings.CORS_ALLOW_HEADERS) == (
        *default_headers,
        "x-session-token",
        "x-email-verification-key",
        "x-password-reset-key",
        "x-device-token",
    )
    assert settings.CORS_ALLOW_CREDENTIALS is False


@pytest.mark.parametrize(
    "path", ["/api/v1/accounts/me", "/_allauth/app/v1/auth/session"]
)
def test_preflight_allows_the_session_token_header(
    allowed_client: Client, path: str
) -> None:
    response = allowed_client.options(
        path,
        HTTP_ORIGIN=ALLOWED,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="x-session-token",
    )

    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == ALLOWED
    allowed = {
        header.strip().lower()
        for header in response["Access-Control-Allow-Headers"].split(",")
    }
    assert "x-session-token" in allowed
    assert "Access-Control-Allow-Credentials" not in response

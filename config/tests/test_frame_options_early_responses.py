"""X-Frame-Options on responses returned before the view (issue #102).

The 413 for an oversized body, a CORS preflight, a static file, and the
https redirect are returned by middleware before any view runs. Each
still refuses framing.
"""

from django.conf import settings
from django.test import Client, override_settings

ALLOWED = "https://app.example.org"


def test_the_body_limit_413_refuses_framing() -> None:
    response = Client().post(
        "/api/v1/matches/",
        data=b"",
        content_type="application/json",
        CONTENT_LENGTH=str(settings.DATA_UPLOAD_MAX_MEMORY_SIZE + 1),
    )
    assert response.status_code == 413
    assert response["X-Frame-Options"] == "DENY"


def test_a_cors_preflight_refuses_framing() -> None:
    with override_settings(CORS_ALLOWED_ORIGINS=[ALLOWED]):
        response = Client().options(
            "/api/v1/health/live",
            HTTP_ORIGIN=ALLOWED,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        )
    assert response["Access-Control-Allow-Origin"] == ALLOWED
    assert response["X-Frame-Options"] == "DENY"


def test_a_static_file_refuses_framing() -> None:
    # WhiteNoise serves the app's static files from the finders here, as
    # it serves the collected files in production.
    with override_settings(WHITENOISE_USE_FINDERS=True, WHITENOISE_AUTOREFRESH=True):
        response = Client().get("/static/admin/css/base.css")
    assert response.status_code == 200
    assert response["X-Frame-Options"] == "DENY"


def test_the_https_redirect_refuses_framing() -> None:
    with override_settings(SECURE_SSL_REDIRECT=True):
        response = Client().get("/api/v1/accounts/me")
    assert response.status_code == 301
    assert response["X-Frame-Options"] == "DENY"

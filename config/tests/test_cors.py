"""Tests for CORS on the API (issue #10).

Browsers on the origins in CORS_ALLOWED_ORIGINS may call /api/. Every
other origin, and every other path, gets no CORS headers. Credentials
are never allowed cross-origin. Settings refuse an entry that is not an
exact http or https origin, without echoing it, because gunicorn runs no
system checks.
"""

from collections.abc import Iterator

import pytest
from django.conf import settings
from django.test import Client, override_settings

from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _run, _settings

LIVE_URL = "/api/v1/health/live"
ALLOWED = "https://app.example.org"
OTHER = "https://other.example.org"

# A made-up host that must never appear in any output.
MARKER = "cors-marker-q7z.example.org"


def _cors_headers(response: object) -> dict[str, str]:
    headers = getattr(response, "headers", {})
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower().startswith("access-control-")
    }


@pytest.fixture
def allowed_client() -> Iterator[Client]:
    with override_settings(CORS_ALLOWED_ORIGINS=[ALLOWED]):
        yield Client()


def test_corsheaders_is_installed() -> None:
    assert "corsheaders" in settings.INSTALLED_APPS


def test_cors_middleware_runs_after_whitenoise_and_before_common() -> None:
    middleware = settings.MIDDLEWARE
    cors = middleware.index("corsheaders.middleware.CorsMiddleware")
    assert cors == middleware.index("whitenoise.middleware.WhiteNoiseMiddleware") + 1
    assert cors < middleware.index("django.middleware.common.CommonMiddleware")


def test_cors_applies_to_api_paths_only() -> None:
    assert settings.CORS_URLS_REGEX == r"^/api/"


def test_credentials_and_wildcard_stay_off() -> None:
    assert settings.CORS_ALLOW_CREDENTIALS is False
    assert settings.CORS_ALLOW_ALL_ORIGINS is False


def test_allowed_origin_gets_the_allow_origin_header(allowed_client: Client) -> None:
    response = allowed_client.get(LIVE_URL, HTTP_ORIGIN=ALLOWED)
    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == ALLOWED
    assert "access-control-allow-credentials" not in _cors_headers(response)


def test_other_origin_gets_no_cors_headers(allowed_client: Client) -> None:
    response = allowed_client.get(LIVE_URL, HTTP_ORIGIN=OTHER)
    assert response.status_code == 200
    assert _cors_headers(response) == {}


@override_settings(CORS_ALLOWED_ORIGINS=[])
def test_empty_list_allows_no_origin(client: Client) -> None:
    response = client.get(LIVE_URL, HTTP_ORIGIN=ALLOWED)
    assert response.status_code == 200
    assert _cors_headers(response) == {}


def test_preflight_from_allowed_origin_gets_allow_headers(
    allowed_client: Client,
) -> None:
    response = allowed_client.options(
        LIVE_URL,
        HTTP_ORIGIN=ALLOWED,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
    )
    assert response.status_code == 200
    headers = _cors_headers(response)
    assert headers["access-control-allow-origin"] == ALLOWED
    assert "GET" in headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in headers


def test_preflight_from_other_origin_gets_no_cors_headers(
    allowed_client: Client,
) -> None:
    response = allowed_client.options(
        LIVE_URL,
        HTTP_ORIGIN=OTHER,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
    )
    assert _cors_headers(response) == {}


def test_allowed_origin_on_a_non_api_path_gets_no_cors_headers(
    allowed_client: Client,
) -> None:
    response = allowed_client.get("/admin/login/", HTTP_ORIGIN=ALLOWED)
    assert response.status_code == 200
    assert _cors_headers(response) == {}


# Settings parsing, each case in a fresh process.


@pytest.mark.parametrize("value", ["", " ", " , "])
def test_unset_or_blank_allows_no_origin(value: str) -> None:
    loaded = _settings({**PRODUCTION_LIKE, "CORS_ALLOWED_ORIGINS": value})
    assert loaded["CORS_ALLOWED_ORIGINS"] == []


def test_local_debug_allows_no_origin() -> None:
    assert _settings(LOCAL_DEBUG)["CORS_ALLOWED_ORIGINS"] == []


def test_origins_are_split_and_stripped() -> None:
    loaded = _settings(
        {
            **PRODUCTION_LIKE,
            "CORS_ALLOWED_ORIGINS": "https://a.example.org, http://localhost:3000",
        }
    )
    assert loaded["CORS_ALLOWED_ORIGINS"] == [
        "https://a.example.org",
        "http://localhost:3000",
    ]


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.org",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
        "https://app.example.org:8443",
        "http://localhost:443",
        "https://localhost:80",
        "http://[::1]:3000",
        "https://xn--bcher-kva.example.org",
    ],
)
def test_exact_origins_are_accepted(origin: str) -> None:
    loaded = _settings({**PRODUCTION_LIKE, "CORS_ALLOWED_ORIGINS": origin})
    assert loaded["CORS_ALLOWED_ORIGINS"] == [origin]


REJECTED_ORIGINS = [
    f"https://*.{MARKER}",
    f"https://{MARKER}/",
    f"https://{MARKER}/app",
    f"https://{MARKER}?x=1",
    f"https://{MARKER}?",
    f"https://{MARKER}#top",
    f"https://user:pw@{MARKER}",
    f"https://user@{MARKER}",
    MARKER,
    f"//{MARKER}",
    f"ftp://{MARKER}",
    f"HTTPS://{MARKER}",
    f"https://{MARKER.upper()}",
    f"https://{MARKER}:",
    f"https://{MARKER}:0",
    f"https://{MARKER}:65536",
    f"https://{MARKER}:080",
    # Browsers leave out the default port, so these could never match.
    f"https://{MARKER}:443",
    f"http://{MARKER}:80",
    f"https://{MARKER}:abc",
    f"https://bad host.{MARKER}",
    f"https://{MARKER}\\@evil.example.org",
    f"https://ok.example.org,https://{MARKER}/",
]


@pytest.mark.parametrize("value", REJECTED_ORIGINS)
def test_bad_origin_fails_naming_the_variable_without_echoing(value: str) -> None:
    result = _run(
        ["manage.py", "check"], {**PRODUCTION_LIKE, "CORS_ALLOWED_ORIGINS": value}
    )
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "CORS_ALLOWED_ORIGINS" in result.stderr
    output = result.stdout + result.stderr
    assert MARKER not in output
    assert MARKER.upper() not in output


@pytest.mark.parametrize("value", ["*", "null", "https://", "http://"])
def test_wildcard_and_hostless_values_fail(value: str) -> None:
    result = _run(
        ["manage.py", "check"], {**PRODUCTION_LIKE, "CORS_ALLOWED_ORIGINS": value}
    )
    assert result.returncode != 0
    error = result.stderr.strip().splitlines()[-1]
    assert error.startswith("django.core.exceptions.ImproperlyConfigured:")
    message = error.split(":", 1)[1]
    assert "CORS_ALLOWED_ORIGINS" in message
    assert value not in message

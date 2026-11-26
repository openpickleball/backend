"""Security headers and cookies under production settings (issue #102).

Settings are read once at import, so each case runs Django's test client
in a fresh Python process with the environment from test_settings.py. The
process overrides only the static files storage (collectstatic has not
run) and, so a session can be saved without a database, the session
engine and the cache. Cookie names and attributes do not depend on them.
"""

import json
from typing import Any

import pytest
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from apps.players.age import age_limit_date
from config.security_headers import DISABLED_FEATURES, PermissionsPolicyMiddleware
from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _run

HOST = "api.example.org"

ENFORCED = "Content-Security-Policy"
REPORT_ONLY = "Content-Security-Policy-Report-Only"

DIRECTIVES = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": "'self'",
    "img-src": "'self' data:",
    "font-src": "'self'",
    "connect-src": "'self'",
    "object-src": "'none'",
    "base-uri": "'none'",
    "form-action": "'self'",
    "frame-ancestors": "'none'",
}

PERMISSIONS_POLICY_FEATURES = (
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

PAGES = ("/api/v1/health/live", "/admin/login/", "/_allauth/openapi.html")

# A refused age saves the signup refusal flag in the session.
AGE_CHECK = "/_allauth/browser/v1/auth/signup/age-check"

# Runs in the subprocess. It prints each page's status, headers, and
# cookies with their attributes as JSON.
DRIVE = """
import json
import sys

import django
from django.conf import settings
from django.test.utils import override_settings

django.setup()
override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    },
    SESSION_ENGINE="django.contrib.sessions.backends.cache",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
).enable()

from django.test import Client

host, secure, age_check, young_year, *pages = sys.argv[1:]
secure = secure == "1"


def describe(response):
    cookies = {}
    for name, morsel in response.cookies.items():
        cookies[name] = {
            "secure": bool(morsel["secure"]),
            "httponly": bool(morsel["httponly"]),
            "path": morsel["path"],
            "domain": morsel["domain"],
            "samesite": morsel["samesite"],
        }
    return {
        "status": response.status_code,
        "headers": dict(response.headers),
        "cookies": cookies,
    }


client = Client(HTTP_HOST=host)
result = {page: describe(client.get(page, secure=secure)) for page in pages}
result[age_check] = describe(
    client.post(
        age_check,
        {"birth_year": int(young_year), "birth_month": 6},
        content_type="application/json",
        secure=secure,
    )
)
print(json.dumps(result))
"""


def _drive(values: dict[str, str], *, host: str, secure: bool) -> dict[str, Any]:
    result = _run(
        [
            "-c",
            DRIVE,
            host,
            "1" if secure else "0",
            AGE_CHECK,
            # Five years old, so always under 13.
            str(age_limit_date().year - 5),
            *PAGES,
        ],
        values,
    )
    assert result.returncode == 0, result.stderr
    loaded: dict[str, Any] = json.loads(result.stdout)
    return loaded


def _policy(header: str) -> dict[str, str]:
    directives = {}
    for part in header.split(";"):
        name, _, value = part.strip().partition(" ")
        directives[name] = value
    return directives


@pytest.fixture(scope="module")
def production() -> dict[str, Any]:
    return _drive(PRODUCTION_LIKE, host=HOST, secure=True)


@pytest.fixture(scope="module")
def production_report_only() -> dict[str, Any]:
    return _drive(
        {**PRODUCTION_LIKE, "CSP_REPORT_ONLY": "true"}, host=HOST, secure=True
    )


@pytest.fixture(scope="module")
def local() -> dict[str, Any]:
    return _drive(LOCAL_DEBUG, host="localhost", secure=False)


@pytest.mark.parametrize("page", PAGES)
def test_pages_load(production: dict[str, Any], page: str) -> None:
    assert production[page]["status"] == 200


@pytest.mark.parametrize("page", PAGES)
def test_enforced_csp_on_every_page(production: dict[str, Any], page: str) -> None:
    headers = production[page]["headers"]
    assert REPORT_ONLY not in headers
    assert _policy(headers[ENFORCED]) == DIRECTIVES


@pytest.mark.parametrize("page", PAGES)
def test_csp_allows_no_inline_eval_or_other_host(
    production: dict[str, Any], page: str
) -> None:
    header = production[page]["headers"][ENFORCED]
    assert "unsafe" not in header
    assert "http" not in header
    assert "*" not in header


@pytest.mark.parametrize("page", PAGES)
def test_standard_headers_on_every_page(production: dict[str, Any], page: str) -> None:
    headers = production[page]["headers"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains; preload"
    )


@pytest.mark.parametrize("page", PAGES)
def test_permissions_policy_turns_off_every_listed_feature(
    production: dict[str, Any], page: str
) -> None:
    header = production[page]["headers"]["Permissions-Policy"]
    features = [part.strip() for part in header.split(",")]
    assert features == [f"{feature}=()" for feature in PERMISSIONS_POLICY_FEATURES]


def test_permissions_policy_leaves_passkeys_and_fullscreen(
    production: dict[str, Any],
) -> None:
    header = production["/admin/login/"]["headers"]["Permissions-Policy"]
    assert "publickey-credentials-get" not in header
    assert "fullscreen" not in header


def test_csrf_cookie_is_a_host_cookie(production: dict[str, Any]) -> None:
    cookies = production["/admin/login/"]["cookies"]
    assert set(cookies) == {"__Host-csrftoken"}
    assert cookies["__Host-csrftoken"] == {
        "secure": True,
        # The browser client reads the token from it.
        "httponly": False,
        "path": "/",
        "domain": "",
        "samesite": "Lax",
    }


def test_session_cookie_is_a_host_cookie(production: dict[str, Any]) -> None:
    response = production[AGE_CHECK]
    assert response["status"] == 400
    assert response["cookies"]["__Host-sessionid"] == {
        "secure": True,
        "httponly": True,
        "path": "/",
        "domain": "",
        "samesite": "Lax",
    }


@pytest.mark.parametrize("page", PAGES)
def test_report_only_mode_sends_only_the_report_only_header(
    production_report_only: dict[str, Any], page: str
) -> None:
    headers = production_report_only[page]["headers"]
    assert ENFORCED not in headers
    assert _policy(headers[REPORT_ONLY]) == DIRECTIVES


@pytest.mark.parametrize("page", PAGES)
def test_debug_on_reports_csp_only(local: dict[str, Any], page: str) -> None:
    headers = local[page]["headers"]
    assert local[page]["status"] == 200
    assert ENFORCED not in headers
    assert _policy(headers[REPORT_ONLY]) == DIRECTIVES


def test_debug_on_keeps_plain_cookie_names(local: dict[str, Any]) -> None:
    assert set(local["/admin/login/"]["cookies"]) == {"csrftoken"}
    assert "sessionid" in local[AGE_CHECK]["cookies"]
    assert local[AGE_CHECK]["cookies"]["sessionid"]["secure"] is False


PRINT_CSP_MODE = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps({
    'enforced': settings.CONTENT_SECURITY_POLICY is not None,
    'report_only': settings.CONTENT_SECURITY_POLICY_REPORT_ONLY is not None,
}))
"""


@pytest.mark.parametrize(
    ("env", "value", "enforced"),
    [
        (PRODUCTION_LIKE, "", True),
        (PRODUCTION_LIKE, "false", True),
        (PRODUCTION_LIKE, "true", False),
        (LOCAL_DEBUG, "", False),
        (LOCAL_DEBUG, "true", False),
        (LOCAL_DEBUG, "false", True),
    ],
)
def test_csp_mode(env: dict[str, str], value: str, *, enforced: bool) -> None:
    result = _run(["-c", PRINT_CSP_MODE], {**env, "CSP_REPORT_ONLY": value})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "enforced": enforced,
        "report_only": not enforced,
    }


def test_the_header_middlewares_wrap_every_other_middleware() -> None:
    assert settings.MIDDLEWARE[:5] == [
        "csp.middleware.CSPMiddleware",
        "config.security_headers.PermissionsPolicyMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
        # Answers the platform's health probes (ADR 0042), inside the headers.
        "config.health_probe.HealthProbeMiddleware",
        "django.middleware.security.SecurityMiddleware",
    ]
    assert (
        settings.MIDDLEWARE.count(
            "django.middleware.clickjacking.XFrameOptionsMiddleware"
        )
        == 1
    )


def test_permissions_policy_keeps_a_header_the_view_set() -> None:
    def view(request: HttpRequest) -> HttpResponse:
        response = HttpResponse()
        response["Permissions-Policy"] = "camera=(self)"
        return response

    request = RequestFactory().get("/")
    response = PermissionsPolicyMiddleware(view)(request)
    assert response["Permissions-Policy"] == "camera=(self)"


def test_permissions_policy_lists_the_features() -> None:
    assert PERMISSIONS_POLICY_FEATURES == DISABLED_FEATURES

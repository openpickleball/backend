"""The request body limit on every route (issue #102).

A declared Content-Length over DATA_UPLOAD_MAX_MEMORY_SIZE gets a 413
ErrorResponse before anything reads the body, signed in or not. A POST
to the CSV results upload keeps its own limit (apps/matches/upload_limits).
"""

import pytest
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory, override_settings

from apps.matches.upload_limits import MAX_REQUEST_BYTES
from config.body_limits import TOO_LARGE_MESSAGE, RequestBodyLimitMiddleware

LIMIT = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
TOO_LARGE = {"code": "too_large", "message": TOO_LARGE_MESSAGE, "errors": []}
UPLOAD = "/api/v1/organizer/events/E-AAAA-AAAA/results-files"
ALLOWED = "https://app.example.org"


class _Unreadable:
    """A request body stream that fails the test if anything reads it."""

    def read(self, *args: object) -> bytes:
        raise AssertionError("the body was read")

    readline = read


def _passed() -> tuple[RequestBodyLimitMiddleware, HttpResponse]:
    passed = HttpResponse("passed")

    def get_response(request: HttpRequest) -> HttpResponse:
        return passed

    return RequestBodyLimitMiddleware(get_response), passed


def _oversized(client: Client, method: str, path: str, **extra: str) -> HttpResponse:
    response: HttpResponse = getattr(client, method)(
        path,
        data=b"",
        content_type="application/json",
        CONTENT_LENGTH=str(LIMIT + 1),
        **{"wsgi.input": _Unreadable()},
        **extra,
    )
    return response


def test_the_limit_is_djangos_default() -> None:
    assert LIMIT == 2_621_440


def test_the_middleware_runs_after_cors_and_before_anything_reads_the_body() -> None:
    middleware = settings.MIDDLEWARE
    limit = middleware.index("config.body_limits.RequestBodyLimitMiddleware")
    assert middleware[limit - 1] == "corsheaders.middleware.CorsMiddleware"
    assert middleware[limit + 1] == (
        "apps.matches.upload_limits.ResultsFileBodyLimitMiddleware"
    )
    assert limit < middleware.index("django.middleware.csrf.CsrfViewMiddleware")
    assert limit < middleware.index(
        "django.contrib.sessions.middleware.SessionMiddleware"
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/_allauth/browser/v1/auth/login"),
        ("post", "/_allauth/app/v1/auth/signup"),
        ("post", "/admin/login/"),
        ("post", "/api/v1/matches/"),
        ("put", "/api/v1/me/player"),
        ("patch", "/api/v1/me/player"),
        ("get", "/api/v1/health/live"),
        ("post", "/oauth/token/"),
        # Only a POST to the upload route keeps its own limit.
        ("get", UPLOAD),
        ("put", UPLOAD),
    ],
)
def test_an_oversized_body_is_refused_unread(method: str, path: str) -> None:
    response = _oversized(Client(), method, path)
    assert response.status_code == 413
    assert response.json() == TOO_LARGE


def test_the_refusal_comes_before_the_csrf_check() -> None:
    client = Client(enforce_csrf_checks=True)
    response = _oversized(client, "post", "/_allauth/browser/v1/auth/login")
    assert response.status_code == 413


def test_the_refusal_carries_cors_headers() -> None:
    with override_settings(CORS_ALLOWED_ORIGINS=[ALLOWED]):
        response = _oversized(Client(), "post", "/api/v1/matches/", HTTP_ORIGIN=ALLOWED)
    assert response.status_code == 413
    assert response["Access-Control-Allow-Origin"] == ALLOWED


@pytest.mark.parametrize("declared", ["0", str(LIMIT), "", "abc", "-5", " 12 "])
def test_a_body_within_the_limit_reaches_the_view(declared: str) -> None:
    middleware, passed = _passed()
    request = RequestFactory().post("/api/v1/matches/", CONTENT_LENGTH=declared)
    assert middleware(request) is passed


def test_no_declared_length_reaches_the_view() -> None:
    middleware, passed = _passed()
    request = RequestFactory().post("/api/v1/matches/")
    del request.META["CONTENT_LENGTH"]
    assert middleware(request) is passed


def test_the_upload_route_keeps_its_own_limit() -> None:
    middleware, passed = _passed()
    for path in (UPLOAD, f"{UPLOAD}/"):
        request = RequestFactory().post(path, CONTENT_LENGTH=str(LIMIT + 1))
        assert middleware(request) is passed


def test_the_upload_route_still_refuses_with_its_own_413() -> None:
    assert MAX_REQUEST_BYTES < LIMIT
    response = _oversized(Client(), "post", UPLOAD)
    assert response.status_code == 413
    assert response.json() != TOO_LARGE
    assert response.json()["code"] == "too_large"


def test_the_limit_follows_the_setting() -> None:
    middleware, passed = _passed()
    with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=10):
        refused = middleware(RequestFactory().post("/", CONTENT_LENGTH="11"))
        allowed = middleware(RequestFactory().post("/", CONTENT_LENGTH="10"))
    assert refused.status_code == 413
    assert allowed is passed


def test_no_limit_when_the_setting_is_none() -> None:
    middleware, passed = _passed()
    with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=None):
        request = RequestFactory().post("/", CONTENT_LENGTH=str(LIMIT * 10))
        assert middleware(request) is passed

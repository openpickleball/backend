"""Malformed JSON bodies on allauth's headless API get 400, not 500 (#337).

allauth's body parser catches only ``UnicodeDecodeError`` and
``JSONDecodeError``, so deep nesting (``RecursionError``) and a number
over Python's integer digit limit (``ValueError``) escaped as server
errors. ``HeadlessJsonErrorMiddleware`` answers them with allauth's own
400. Made-up data only.
"""

import logging

import pytest
from allauth.headless.internal.restkit.views import RESTView
from django.http import HttpRequest, HttpResponseBadRequest
from django.test import Client, RequestFactory

from apps.accounts.middleware import HeadlessJsonErrorMiddleware
from apps.accounts.tests.headless import APP, BROWSER, browser_client, csrf_header

pytestmark = pytest.mark.django_db

DEEP_LIST = "[" * 100_000
DEEP_OBJECT = '{"a":' * 100_000
LONG_NUMBER = "1" * 5000

# Bodies allauth's parser raised on before the fix.
UNPARSABLE = {
    "deep list": (DEEP_LIST, "application/json"),
    "deep object": (DEEP_OBJECT, "application/json"),
    "long number": (LONG_NUMBER, "application/json"),
    "long number in object": ('{"email": ' + LONG_NUMBER + "}", "application/json"),
    "deep list as text": (DEEP_LIST, "text/plain"),
}

# Bodies allauth already refused with a 400, kept that way.
ALREADY_REFUSED = {
    "bad json": ("{not json", "application/json"),
    "empty body": ("", "application/json"),
    "form body": ("email=a%40example.org", "application/x-www-form-urlencoded"),
    "invalid utf-8": (b"\xff\xfe", "application/json"),
    "not an object": ("[1]", "application/json"),
}

# allauth's views, and OPEN's replacements that call its parser first.
ENDPOINTS = [
    "auth/login",
    "auth/signup",
    "auth/signup/age-check",
    "auth/signup/password-check",
    "auth/password/request",
]


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("base", [APP, BROWSER])
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("body", [*UNPARSABLE, *ALREADY_REFUSED])
def test_malformed_body_gets_400(
    caplog: pytest.LogCaptureFixture, base: str, endpoint: str, body: str
) -> None:
    data, content_type = {**UNPARSABLE, **ALREADY_REFUSED}[body]

    # The test client raises any exception the view did not handle.
    with caplog.at_level(logging.DEBUG):
        response = Client().post(f"{base}/{endpoint}", data, content_type=content_type)

    assert response.status_code == 400
    assert _errors(caplog) == []


@pytest.mark.parametrize("body", list(UNPARSABLE))
def test_answer_is_allauths_own_bad_request(body: str) -> None:
    data, content_type = UNPARSABLE[body]
    refused = Client().post(
        f"{APP}/auth/login", "{not json", content_type="application/json"
    )

    response = Client().post(f"{APP}/auth/login", data, content_type=content_type)

    assert response.status_code == refused.status_code == 400
    assert response.content == refused.content
    assert response["Content-Type"] == refused["Content-Type"]


def test_browser_csrf_check_still_runs_first() -> None:
    client = browser_client()

    response = client.post(
        f"{BROWSER}/auth/login", DEEP_LIST, content_type="application/json"
    )

    assert response.status_code == 403


def test_browser_with_csrf_token_gets_400() -> None:
    client = browser_client()

    response = client.post(
        f"{BROWSER}/auth/login",
        DEEP_LIST,
        content_type="application/json",
        headers=csrf_header(client),
    )

    assert response.status_code == 400


def test_malformed_bodies_still_count_toward_the_rate_limit() -> None:
    # ACCOUNT_RATE_LIMITS["signup"] is 20/m/ip, checked before the parse.
    client = Client()
    for _ in range(20):
        response = client.post(
            f"{APP}/auth/signup", DEEP_LIST, content_type="application/json"
        )
        assert response.status_code == 400

    response = client.post(
        f"{APP}/auth/signup", DEEP_LIST, content_type="application/json"
    )

    assert response.status_code == 429


def _raise_from_parse_json(exception: Exception) -> Exception:
    """``exception`` raised from inside allauth's body parser."""
    view = RESTView()
    request = RequestFactory().post("/", "{}", content_type="application/json")

    def loads(_text: str) -> None:
        raise exception

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("allauth.headless.internal.restkit.views.json.loads", loads)
        try:
            view._parse_json(request)
        except type(exception) as raised:
            return raised
    pytest.fail("the parser did not raise")


def _middleware() -> HeadlessJsonErrorMiddleware:
    return HeadlessJsonErrorMiddleware(lambda request: HttpResponseBadRequest())


@pytest.mark.parametrize("exception", [RecursionError(), ValueError("too many digits")])
def test_parser_errors_become_400(exception: Exception) -> None:
    raised = _raise_from_parse_json(exception)

    response = _middleware().process_exception(HttpRequest(), raised)

    assert response is not None
    assert response.status_code == 400


def test_other_exceptions_from_the_parser_are_left_alone() -> None:
    raised = _raise_from_parse_json(KeyError("not a decoding error"))

    assert _middleware().process_exception(HttpRequest(), raised) is None


@pytest.mark.parametrize("exception", [RecursionError(), ValueError("elsewhere")])
def test_errors_from_elsewhere_are_left_alone(exception: Exception) -> None:
    try:
        raise exception
    except type(exception) as raised:
        caught = raised

    assert _middleware().process_exception(HttpRequest(), caught) is None

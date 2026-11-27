"""Account middleware (#100, ADR 0047, #337)."""

from collections.abc import Callable
from types import CodeType
from typing import Final

from allauth.headless.internal.restkit.views import RESTView
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponseBadRequest, HttpResponseBase

from apps.accounts.devices import write_marker
from apps.accounts.sessions import check_lifetime, end_session


class SessionLifetimeMiddleware:
    """Ends a browser's signed-in session past its lifetime (#100).

    It runs after ``AuthenticationMiddleware``. An ended session is
    deleted, with its user session row, and the request goes on signed
    out. App session tokens are checked when allauth's token strategy
    looks them up (``apps.accounts.token_strategy``).
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        session = getattr(request, "session", None)
        if session is not None and not check_lifetime(session):
            end_session(session)
            request.user = AnonymousUser()
        return self.get_response(request)


class DeviceMarkerMiddleware:
    """Writes the device marker a completed login left on the request
    (``apps.accounts.devices``, #100): the browser client's cookie, or
    the app client's ``X-Device-Token`` response header."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        write_marker(request, response)
        return response


# The code of allauth's headless body parser, which every headless view
# that reads JSON calls (OPEN's replacements in headless_views included).
_PARSE_JSON_CODE: Final[CodeType] = RESTView._parse_json.__code__


def _raised_in_parse_json(exception: BaseException) -> bool:
    """Whether ``exception`` came out of allauth's headless body parser."""
    frame = exception.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code is _PARSE_JSON_CODE:
            return True
        frame = frame.tb_next
    return False


class HeadlessJsonErrorMiddleware:
    """Answers 400 for a body allauth's headless parser cannot decode (#337).

    allauth's ``RESTView._parse_json`` catches only ``UnicodeDecodeError``
    and ``JSONDecodeError``. ``json.loads`` raises ``RecursionError`` for
    deep nesting and ``ValueError`` for a number over Python's integer
    digit limit, and those escaped as 500s that anyone could cause. This
    answers them with the bare 400 allauth gives every other malformed
    body, so nothing is logged at error level and no exception reaches
    error tracking.

    It acts only after the view raised, and only on an exception raised
    inside the parser, so the CSRF check, allauth's rate limits (which
    count the request before the parse), the body size limit, and auth run
    as before, and any other error stays a 500. Remove it once allauth's
    parser catches both errors itself.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        return self.get_response(request)

    def process_exception(
        self, request: HttpRequest, exception: Exception
    ) -> HttpResponseBase | None:
        if isinstance(exception, RecursionError | ValueError) and _raised_in_parse_json(
            exception
        ):
            return HttpResponseBadRequest()
        return None

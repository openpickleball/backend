"""Account middleware (#100, ADR 0047)."""

from collections.abc import Callable

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponseBase

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

"""Hold the request's Account as the actor for Audit Events."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponseBase

from apps.audit.actor import acting_as


class AuditActorMiddleware:
    """Run each request with ``request.user`` as the current actor.

    It must come after ``AuthenticationMiddleware``, which sets
    ``request.user``. The user is read only when an event is recorded.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        with acting_as(getattr(request, "user", None)):
            return self.get_response(request)

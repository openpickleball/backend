"""The request body limit on every route (issue #102, ADR 0040).

Django refuses a form or JSON body over ``DATA_UPLOAD_MAX_MEMORY_SIZE``
only once something reads it, and it does not count the files of a
multipart body, which it spools to disk before the view runs. So
``RequestBodyLimitMiddleware`` refuses a declared ``Content-Length`` over
that setting with the 413 ``ErrorResponse`` before any byte of the body
is read, on every path and method, signed in or not. Under WSGI, Django
reads at most ``Content-Length`` bytes, and none without it, so this
bounds every body. A ``Content-Length`` that is not a whole number, or is
negative, counts as 0, since Django then reads no body.

A POST to the CSV results upload route keeps its own limit and its own
413 (``apps.matches.upload_limits``). With the setting None, as Django
allows, nothing is refused here.

The middleware comes right after ``CorsMiddleware``, so the 413 carries
CORS headers, and before every middleware that may read the body
(``CsrfViewMiddleware``).
"""

from collections.abc import Callable
from typing import Final

from django.conf import settings
from django.http import HttpRequest, HttpResponseBase, JsonResponse

from apps.matches.upload_limits import TOO_LARGE_STATUS, UPLOAD_PATH

TOO_LARGE_MESSAGE: Final = "The request body is too large."


def _declared_length(request: HttpRequest) -> int:
    """The declared ``Content-Length``, or 0 when Django would read none."""
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return 0
    return max(declared, 0)


class RequestBodyLimitMiddleware:
    """Refuses a declared body over ``DATA_UPLOAD_MAX_MEMORY_SIZE``."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        if (
            limit is not None
            and _declared_length(request) > limit
            and not (
                request.method == "POST" and UPLOAD_PATH.fullmatch(request.path_info)
            )
        ):
            return JsonResponse(
                {"code": "too_large", "message": TOO_LARGE_MESSAGE, "errors": []},
                status=TOO_LARGE_STATUS,
            )
        return self.get_response(request)

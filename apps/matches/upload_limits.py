"""The request body limit of the CSV results upload (#71).

Django parses a multipart body, and spools its files to disk, before the
view runs. So ``ResultsFileBodyLimitMiddleware``, for a POST to the
upload route only, refuses a declared ``Content-Length`` over
``MAX_REQUEST_BYTES`` (the file limit plus a fixed allowance for the
multipart framing and the form fields) with the 413 ``ErrorResponse``
before any byte of the body is read, signed in or not. Otherwise it puts
``ResultsFileUploadHandler`` first in the request's upload handlers, so a
file that streams past ``MAX_FILE_BYTES`` stops with the same 413 (ninja
turns the ``HttpError`` it raises into the ``ErrorResponse``). Under
WSGI, Django reads no body at all without a ``Content-Length``. The
view's own size checks stay.

The middleware comes before every middleware that may read the body
(``CsrfViewMiddleware``), and answers no other route.
"""

import re
from collections.abc import Callable
from typing import Final

from django.core.files.uploadhandler import FileUploadHandler
from django.http import HttpRequest, HttpResponseBase, JsonResponse
from ninja.errors import HttpError

from apps.matches.results_files import FILE_TOO_LARGE, MAX_FILE_BYTES

# The multipart framing, headers, and the small form fields around the
# file.
MULTIPART_ALLOWANCE: Final = 65_536
MAX_REQUEST_BYTES: Final = MAX_FILE_BYTES + MULTIPART_ALLOWANCE

UPLOAD_PATH: Final = re.compile(r"/api/v1/organizer/events/[^/]+/results-files/?")
TOO_LARGE_STATUS: Final = 413


def _too_large() -> JsonResponse:
    """The 413 ``ErrorResponse``, as ``config.api_errors`` gives it."""
    return JsonResponse(
        {"code": "too_large", "message": FILE_TOO_LARGE, "errors": []},
        status=TOO_LARGE_STATUS,
    )


class ResultsFileUploadHandler(FileUploadHandler):
    """Stops an uploaded file once it passes ``MAX_FILE_BYTES``.

    It passes every chunk on to the next handler unchanged, and leaves
    storing the file to them.
    """

    def receive_data_chunk(self, raw_data: bytes, start: int) -> bytes:
        if start + len(raw_data) > MAX_FILE_BYTES:
            raise HttpError(TOO_LARGE_STATUS, FILE_TOO_LARGE)
        return raw_data

    def file_complete(self, file_size: int) -> None:
        return None


class ResultsFileBodyLimitMiddleware:
    """Limits the body of a POST to the CSV results upload route."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if request.method == "POST" and UPLOAD_PATH.fullmatch(request.path_info):
            try:
                declared = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                declared = 0
            if declared > MAX_REQUEST_BYTES:
                return _too_large()
            request.upload_handlers.insert(0, ResultsFileUploadHandler(request))
        return self.get_response(request)

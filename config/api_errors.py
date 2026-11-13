"""The error response schema shared by every API operation, and its handlers.

Errors raised while an operation runs (input that fails validation, a body
that does not parse, `Http404`, any `HttpError`) come back as an
`ErrorResponse`:

    {"code": "...", "message": "...", "errors": [{"field", "code", "message"}]}

`errors` lists one item per invalid input for a validation error and is empty
otherwise. Each item copies only pydantic's location, error type, and
message, never its `input` or `ctx`. The message is pydantic's own text, so a
custom validator must not put the submitted value in its error message.

These handlers do not see a wrong method on a known path (ninja answers with
a plain-text 405) or a path that matches no operation (Django's 404 page).
Unexpected exceptions keep ninja's default handler, which re-raises when
DEBUG is off so Django's own 500 handling applies.
"""

from collections.abc import Mapping
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError, ValidationError

VALIDATION_ERROR_STATUS = 422

_STATUS_CODES: Mapping[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "throttled",
}


class FieldError(Schema):
    """One invalid input: where it is, what kind of error, and a message."""

    field: str
    code: str
    message: str


class ErrorResponse(Schema):
    """The body of every error response."""

    code: str
    message: str
    errors: list[FieldError]


def _field_path(loc: list[Any] | tuple[Any, ...]) -> str:
    """Join an error location into a dotted path (ints become strings)."""
    return ".".join(str(part) for part in loc)


def _field_error(error: Mapping[str, Any]) -> FieldError:
    # Copy only the location, type, and message. Never `input` or `ctx`,
    # which can echo what the client sent back to it. The message itself is
    # pydantic's text: custom validators must keep the value out of it.
    return FieldError(
        field=_field_path(error.get("loc", [])),
        code=str(error.get("type", "invalid")),
        message=str(error.get("msg", "")),
    )


def _respond(
    api: NinjaAPI, request: HttpRequest, status: int, body: ErrorResponse
) -> HttpResponse:
    return api.create_response(request, body.model_dump(), status=status)


def install_error_handlers(api: NinjaAPI) -> None:
    """Replace ninja's default handlers with ones that return `ErrorResponse`."""

    @api.exception_handler(ValidationError)
    def on_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
        body = ErrorResponse(
            code="validation_error",
            message="The request is not valid.",
            errors=[_field_error(error) for error in exc.errors],
        )
        return _respond(api, request, VALIDATION_ERROR_STATUS, body)

    @api.exception_handler(HttpError)
    def on_http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
        body = ErrorResponse(
            code=_STATUS_CODES.get(exc.status_code, "error"),
            message=str(exc),
            errors=[],
        )
        return _respond(api, request, exc.status_code, body)

    @api.exception_handler(Http404)
    def on_not_found(request: HttpRequest, exc: Http404) -> HttpResponse:
        # Never include the exception text: it can name internal details.
        body = ErrorResponse(code="not_found", message="Not found.", errors=[])
        return _respond(api, request, 404, body)

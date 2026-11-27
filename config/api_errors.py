"""The error response schema shared by every API operation, and its handlers.

Errors raised while an operation runs (input that fails validation, a body
that does not parse, `Http404`, any `HttpError`,
`apps.matches.services.DuplicateMatchError`,
`apps.matches.services.MatchValidationError`,
`apps.matches.permissions.MatchPermissionDenied`,
`apps.matches.permissions.MatchActionDenied`, and
`apps.matches.workflow.MatchStateError`) come back as an `ErrorResponse`:

    {"code": "...", "message": "...", "errors": [{"field", "code", "message"}]}

A duplicate match item may also carry `existing_code` (see below). No
other item has that key.

`errors` lists one item per invalid input for a validation error (a
`MatchValidationError` gives one item per `MatchIssue`, with its field,
code, and fixed message), one item for a duplicate match (409), and is
empty otherwise. For a pydantic error,
an item copies only pydantic's location, error type, and message, never its
`input` or `ctx`. The message is
pydantic's own text, so a custom validator must not put the submitted value
in its error message. A plain `{name: message}` error (ninja's cursor
pagination raises these) gives one item per key, with code "invalid".

A `DuplicateMatchError` returns 409 with code "duplicate_match" and one
item: field `match` and code `duplicate_match` for an exact duplicate, or
field `external_id` and code `duplicate_external_id` for a repeated
external id. The message is fixed text, never a submitted value. It
names the earlier match's OPEN code only when `existing_code` is set: the
submitter sent that match, or the submitting Account's Player played in
it. A repeated external id always names it. When the message names the
code, the item also carries it as `existing_code`, so a client need not
parse the message. When it does not, the item has no `existing_code` key.

A `MatchPermissionDenied` returns 403 with code "forbidden", fixed text,
and no items. It never says which rule failed.

A `MatchActionDenied` returns 403 with code "forbidden", fixed text, and
no items. A `MatchStateError` returns 409 with code "conflict", fixed
text, and no items. Neither says which rule or state refused the change.

A request that an operation's `auth` does not authenticate (ninja's
`AuthenticationError`, see `config/auth.py`) returns 401 with code
"unauthorized", fixed text, and no items.

An Account that must set up two-factor authentication and has not
(`config.auth.MFARequired`, ADR 0043) gets 403 with code "mfa_required",
fixed text, and no items, on every signed-in operation.

An operation that needs a recent reauthentication (Account deletion,
`config.auth.ReauthenticationRequired`, #116) gets 403 with code
"reauthentication_required", fixed text, and no items. The Organizer and
Club admin changes and the results file upload (#232, ADR 0057) get the
same code when the session's latest two-factor step is older than
`ORGANIZER_STEP_UP_SECONDS`, with the fixed text
`config.auth.STEP_UP_MESSAGE`, before any lookup of the path's records.

An `HttpError` takes its code from its status: 413 (a file over an
upload's size limit) is "too_large".

A request a throttle refuses (ninja's `Throttled`, an `HttpError`, see
`config/throttling.py`) returns 429 with code "throttled", ninja's fixed
text, and no items. ninja sets the `Retry-After` header.

These handlers do not see a wrong method on a known path (ninja answers with
a plain-text 405) or a path that matches no operation (Django's 404 page).
Unexpected exceptions keep ninja's default handler, which re-raises when
DEBUG is off so Django's own 500 handling applies.
"""

from collections.abc import Mapping
from typing import Any

from django.core.exceptions import NON_FIELD_ERRORS
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404, HttpRequest, HttpResponse
from ninja import NinjaAPI, Schema
from ninja.errors import AuthenticationError, HttpError, ValidationError

from apps.matches.permissions import MatchActionDenied, MatchPermissionDenied
from apps.matches.services import (
    DuplicateKind,
    DuplicateMatchError,
    MatchValidationError,
)
from apps.matches.workflow import MatchStateError
from config.auth import (
    MFA_REQUIRED_STATUS,
    REAUTHENTICATION_REQUIRED_STATUS,
    MFARequired,
    ReauthenticationRequired,
)

VALIDATION_ERROR_STATUS = 422
UNAUTHORIZED_STATUS = 401
THROTTLED_STATUS = 429
UNAUTHORIZED_MESSAGE = "Authentication required."
DUPLICATE_MATCH_STATUS = 409
FORBIDDEN_STATUS = 403
CONFLICT_STATUS = 409
VALIDATION_ERROR_MESSAGE = "The request is not valid."

_DUPLICATE_FIELDS: Mapping[DuplicateKind, str] = {
    DuplicateKind.DUPLICATE_MATCH: "match",
    DuplicateKind.DUPLICATE_EXTERNAL_ID: "external_id",
}

_STATUS_CODES: Mapping[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    429: "throttled",
}


class FieldError(Schema):
    """One invalid input: where it is, what kind of error, and a message.

    `existing_code` is the earlier match's OPEN code. Only a duplicate match
    item has it, and only when the submitter may know that match.
    """

    field: str
    code: str
    message: str
    # `_respond` leaves out every None field, so items without a code have no
    # `existing_code` key. A new nullable field here would be left out too.
    existing_code: str | None = None


class ErrorResponse(Schema):
    """The body of every error response."""

    code: str
    message: str
    errors: list[FieldError]


def _field_path(loc: list[Any] | tuple[Any, ...]) -> str:
    """Join an error location into a dotted path (ints become strings)."""
    return ".".join(str(part) for part in loc)


def _field_errors(error: Mapping[str, Any]) -> list[FieldError]:
    """Turn one item of a ninja `ValidationError` into field errors."""
    if "loc" not in error:
        # A plain `{name: message}` item, as ninja's cursor pagination raises.
        return [
            FieldError(field=str(name), code="invalid", message=str(message))
            for name, message in error.items()
        ]
    # A pydantic error. Copy only the location, type, and message. Never
    # `input` or `ctx`, which can echo what the client sent back to it. The
    # message itself is pydantic's text: custom validators must keep the
    # value out of it.
    return [
        FieldError(
            field=_field_path(error["loc"]),
            code=str(error.get("type", "invalid")),
            message=str(error.get("msg", "")),
        )
    ]


def model_validation_error(
    error: DjangoValidationError, prefix: tuple[str, ...] = ("body",)
) -> ValidationError:
    """A model ``ValidationError`` as the API's 422, located under ``prefix``.

    Each field's errors go under ``(*prefix, field)``; non-field errors under
    ``prefix``. Only the model's own messages are copied, so a model whose
    messages could repeat a submitted value must override them first
    (CLAUDE.md, API).
    """
    by_field = (
        error.error_dict
        if hasattr(error, "error_dict")
        else {NON_FIELD_ERRORS: error.error_list}
    )
    items: list[dict[str, object]] = []
    for name, field_errors in by_field.items():
        loc = prefix if name == NON_FIELD_ERRORS else (*prefix, name)
        for item in field_errors:
            items.extend(
                {"loc": loc, "type": item.code or "invalid", "msg": message}
                for message in item
            )
    return ValidationError(items)


def _respond(
    api: NinjaAPI, request: HttpRequest, status: int, body: ErrorResponse
) -> HttpResponse:
    # Leave out None fields: only `existing_code` can be None, and an item
    # without a code must not carry the key.
    return api.create_response(
        request, body.model_dump(exclude_none=True), status=status
    )


def install_error_handlers(api: NinjaAPI) -> None:
    """Replace ninja's default handlers with ones that return `ErrorResponse`."""

    @api.exception_handler(ValidationError)
    def on_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
        body = ErrorResponse(
            code="validation_error",
            message=VALIDATION_ERROR_MESSAGE,
            errors=[item for error in exc.errors for item in _field_errors(error)],
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

    @api.exception_handler(MFARequired)
    def on_mfa_required(request: HttpRequest, exc: MFARequired) -> HttpResponse:
        # Fixed text: never which rule made a method required.
        body = ErrorResponse(code="mfa_required", message=str(exc), errors=[])
        return _respond(api, request, MFA_REQUIRED_STATUS, body)

    @api.exception_handler(ReauthenticationRequired)
    def on_reauthentication_required(
        request: HttpRequest, exc: ReauthenticationRequired
    ) -> HttpResponse:
        body = ErrorResponse(
            code="reauthentication_required", message=str(exc), errors=[]
        )
        return _respond(api, request, REAUTHENTICATION_REQUIRED_STATUS, body)

    @api.exception_handler(AuthenticationError)
    def on_authentication_error(
        request: HttpRequest, exc: AuthenticationError
    ) -> HttpResponse:
        # Fixed text: never which credential was missing or why it failed.
        body = ErrorResponse(
            code="unauthorized", message=UNAUTHORIZED_MESSAGE, errors=[]
        )
        return _respond(api, request, UNAUTHORIZED_STATUS, body)

    @api.exception_handler(Http404)
    def on_not_found(request: HttpRequest, exc: Http404) -> HttpResponse:
        # Never include the exception text: it can name internal details.
        body = ErrorResponse(code="not_found", message="Not found.", errors=[])
        return _respond(api, request, 404, body)

    @api.exception_handler(DuplicateMatchError)
    def on_duplicate_match(
        request: HttpRequest, exc: DuplicateMatchError
    ) -> HttpResponse:
        # Fixed text, plus the earlier match's code when the caller may know it.
        body = ErrorResponse(
            code="duplicate_match",
            message=exc.message,
            errors=[
                FieldError(
                    field=_DUPLICATE_FIELDS[exc.kind],
                    code=exc.kind.value,
                    message=exc.message,
                    existing_code=exc.existing_code,
                )
            ],
        )
        return _respond(api, request, DUPLICATE_MATCH_STATUS, body)

    @api.exception_handler(MatchPermissionDenied)
    def on_match_permission_denied(
        request: HttpRequest, exc: MatchPermissionDenied
    ) -> HttpResponse:
        # Fixed text only: never which rule failed.
        body = ErrorResponse(code="forbidden", message=exc.message, errors=[])
        return _respond(api, request, FORBIDDEN_STATUS, body)

    @api.exception_handler(MatchValidationError)
    def on_match_validation_error(
        request: HttpRequest, exc: MatchValidationError
    ) -> HttpResponse:
        # Each issue's message is fixed text: never a submitted value.
        body = ErrorResponse(
            code="validation_error",
            message=VALIDATION_ERROR_MESSAGE,
            errors=[
                FieldError(field=issue.field, code=issue.code, message=issue.message)
                for issue in exc.issues
            ],
        )
        return _respond(api, request, VALIDATION_ERROR_STATUS, body)

    @api.exception_handler(MatchActionDenied)
    def on_match_action_denied(
        request: HttpRequest, exc: MatchActionDenied
    ) -> HttpResponse:
        # Fixed text only: never which rule failed or the match's state.
        body = ErrorResponse(code="forbidden", message=exc.message, errors=[])
        return _respond(api, request, FORBIDDEN_STATUS, body)

    @api.exception_handler(MatchStateError)
    def on_match_state_error(
        request: HttpRequest, exc: MatchStateError
    ) -> HttpResponse:
        body = ErrorResponse(code="conflict", message=exc.message, errors=[])
        return _respond(api, request, CONFLICT_STATUS, body)

"""The OPEN HTTP API: the single django-ninja instance, mounted at /api/v1/.

`config/urls.py` mounts `api` once. The OpenAPI document is served at
/api/v1/openapi.json and the interactive docs at /api/v1/docs.

Adding an app's endpoints (one router per app):

1. In `apps/<app>/api.py`, define the app's router and its operations:

       from ninja import Router

       router = Router(tags=["<app>"])

       @router.get("/", response=list[ThingOut], operation_id="list_things")
       def list_things(request: HttpRequest) -> ...: ...

2. In this module, mount it under a prefix:

       from apps.<app>.api import router as <app>_router

       api.add_router("<prefix>/", <app>_router)

Every operation needs an explicit `operation_id`: it names the method in the
generated client, so it must not change when a view is renamed. Building the
OpenAPI document raises `ImproperlyConfigured` for an operation without one.

Every error ninja handles comes back as `ErrorResponse` (see
`config/api_errors.py`), and every operation that takes input documents a 422
response with that schema.
"""

from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from ninja import NinjaAPI
from ninja.errors import ValidationError, ValidationErrorContext
from ninja.openapi.schema import OpenAPISchema
from ninja.schema import NinjaGenerateJsonSchema

from config.api_errors import (
    VALIDATION_ERROR_STATUS,
    ErrorResponse,
    install_error_handlers,
)

_SCHEMA_REF = "#/components/schemas/{model}"

if TYPE_CHECKING:
    from ninja.operation import Operation


class OpenNinjaAPI(NinjaAPI):
    """NinjaAPI with OPEN's error schema and required operation ids."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        install_error_handlers(self)

    def validation_error_from_error_contexts(
        self, error_contexts: list[ValidationErrorContext[Any]]
    ) -> ValidationError:
        errors: list[dict[str, Any]] = []
        for context in error_contexts:
            single_attr = getattr(context.model, "__read_from_single_attr__", None)
            for error in super().validation_error_from_error_contexts([context]).errors:
                errors.append(_wire_location(error, single_attr))
        return ValidationError(errors)

    def get_openapi_schema(
        self,
        *,
        path_prefix: str | None = None,
        path_params: dict[str, Any] | None = None,
    ) -> OpenAPISchema:
        schema = super().get_openapi_schema(
            path_prefix=path_prefix, path_params=path_params
        )
        _document_errors(schema)
        return schema

    def get_openapi_operation_id(self, operation: "Operation") -> str:
        # ninja calls this only for an operation declared without an
        # operation_id. Its default invents one from the function name, which
        # would change the generated client whenever a view is renamed.
        view = operation.view_func
        raise ImproperlyConfigured(
            f"API operation {view.__module__}.{view.__qualname__} has no "
            "operation_id. Pass operation_id=... to its route decorator."
        )


def _wire_location(error: dict[str, Any], single_attr: str | None) -> dict[str, Any]:
    """Make a body error's location name the key the client sent.

    When an operation has one body parameter, ninja reads the whole body into
    it and puts the parameter's Python name second in the location
    (`["body", "payload", "items", 0]`). The client never sent that name, so
    it is dropped (`["body", "items", 0]`). With several body parameters the
    second item is a key in the body and stays.
    """
    loc = tuple(error.get("loc", ()))
    if single_attr and len(loc) > 1 and loc[0] == "body" and loc[1] == single_attr:
        return {**error, "loc": (loc[0], *loc[2:])}
    return error


def _document_errors(schema: OpenAPISchema) -> None:
    """Add `ErrorResponse` and a 422 response to every operation with input."""
    error_schema = ErrorResponse.model_json_schema(
        ref_template=_SCHEMA_REF, schema_generator=NinjaGenerateJsonSchema
    )
    definitions = {
        **error_schema.pop("$defs", {}),
        ErrorResponse.__name__: error_schema,
    }
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in definitions.items():
        if name in schemas and schemas[name] != definition:
            raise ImproperlyConfigured(
                f"An API schema named {name} clashes with the shared error schema "
                "of the same name. Rename the API schema."
            )
        schemas[name] = definition

    error_ref = {"$ref": _SCHEMA_REF.format(model=ErrorResponse.__name__)}
    response = {
        "description": "The request is not valid.",
        "content": {"application/json": {"schema": error_ref}},
    }
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            has_input = bool(operation.get("parameters")) or "requestBody" in operation
            if not has_input:
                continue
            responses = operation.setdefault("responses", {})
            # ninja keys responses by int status; JSON rendering makes them strings.
            declared = [
                body
                for code, body in responses.items()
                if str(code) == str(VALIDATION_ERROR_STATUS)
            ]
            if not declared:
                responses[VALIDATION_ERROR_STATUS] = response
                continue
            content = declared[0].get("content", {}).get("application/json", {})
            if content.get("schema") != error_ref:
                raise ImproperlyConfigured(
                    f"API operation {operation.get('operationId')} declares its own "
                    f"{VALIDATION_ERROR_STATUS} response, but input validation "
                    f"always returns {ErrorResponse.__name__}. Remove it or "
                    f"declare {ErrorResponse.__name__}."
                )


api = OpenNinjaAPI(
    title="OPEN API",
    version="0.1.0",
    description=(
        "The free and open API of OPEN, the Open Pickleball Evaluation Network. "
        "No OPEN rating is live yet: any rating it serves is a demonstration rating."
    ),
    urls_namespace="api-v1",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

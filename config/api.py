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
from ninja.openapi.schema import OpenAPISchema

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


def _document_errors(schema: OpenAPISchema) -> None:
    """Add `ErrorResponse` and a 422 response to every operation with input."""
    error_schema = ErrorResponse.model_json_schema(ref_template=_SCHEMA_REF)
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas.update(error_schema.pop("$defs", {}))
    schemas[ErrorResponse.__name__] = error_schema

    response = {
        "description": "The request is not valid.",
        "content": {
            "application/json": {
                "schema": {"$ref": _SCHEMA_REF.format(model=ErrorResponse.__name__)}
            }
        },
    }
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            has_input = bool(operation.get("parameters")) or "requestBody" in operation
            responses = operation.setdefault("responses", {})
            # ninja keys responses by int status; JSON rendering makes them strings.
            documented = {str(code) for code in responses}
            if has_input and str(VALIDATION_ERROR_STATUS) not in documented:
                responses[VALIDATION_ERROR_STATUS] = response


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

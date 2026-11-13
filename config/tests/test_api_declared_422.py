"""An operation may not document a 422 body that validation never returns."""

import uuid

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from ninja import Schema

from config.api import OpenNinjaAPI
from config.api_errors import ErrorResponse


class _OtherError(Schema):
    detail: str


def _api() -> OpenNinjaAPI:
    return OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")


def test_other_422_schema_on_operation_with_input_fails() -> None:
    api = _api()

    @api.get(
        "/things/{n}",
        operation_id="get_thing_custom_422",
        response={200: int, 422: _OtherError},
    )
    def get_thing(request: HttpRequest, n: int) -> int:
        return n

    with pytest.raises(ImproperlyConfigured, match="get_thing_custom_422"):
        api.get_openapi_schema(path_prefix="")


def test_error_response_as_declared_422_is_allowed() -> None:
    api = _api()

    @api.get(
        "/things/{n}",
        operation_id="get_thing_error_422",
        response={200: int, 422: ErrorResponse},
    )
    def get_thing(request: HttpRequest, n: int) -> int:
        return n

    schema = api.get_openapi_schema(path_prefix="")
    responses = schema["paths"]["/things/{n}"]["get"]["responses"]

    assert responses[422]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }

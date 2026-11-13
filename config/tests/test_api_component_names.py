"""The shared error schemas never silently replace an app schema's definition."""

import uuid

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from ninja import Schema

from config.api import OpenNinjaAPI


class ErrorResponse(Schema):
    """An app schema that happens to share the error schema's name."""

    mine: int


class FieldError(Schema):
    """An app schema that happens to share the error item schema's name."""

    other: str


def _api() -> OpenNinjaAPI:
    return OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")


def test_app_schema_named_error_response_fails() -> None:
    api = _api()

    @api.get("/mine", operation_id="get_mine", response={200: ErrorResponse})
    def get_mine(request: HttpRequest) -> dict[str, int]:
        return {"mine": 1}

    with pytest.raises(ImproperlyConfigured, match="ErrorResponse"):
        api.get_openapi_schema(path_prefix="")


def test_app_schema_named_field_error_fails() -> None:
    api = _api()

    @api.get("/other", operation_id="get_other", response={200: FieldError})
    def get_other(request: HttpRequest) -> dict[str, str]:
        return {"other": "x"}

    with pytest.raises(ImproperlyConfigured, match="FieldError"):
        api.get_openapi_schema(path_prefix="")

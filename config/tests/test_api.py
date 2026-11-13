"""Tests for the django-ninja API mounted at /api/v1/ (issue #5)."""

import json
import uuid
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404, HttpRequest
from django.test import Client
from jsonschema import Draft202012Validator
from ninja import Schema
from ninja.errors import HttpError
from ninja.operation import Operation
from ninja.responses import NinjaJSONEncoder
from ninja.testing import TestClient
from openapi_spec_validator import validate
from pytest_django.fixtures import Settings

from config.api import OpenNinjaAPI, api
from config.api_errors import ErrorResponse

OPENAPI_URL = "/api/v1/openapi.json"


def _openapi_document(client: Client) -> dict[str, Any]:
    response = client.get(OPENAPI_URL)
    assert response.status_code == 200
    document: dict[str, Any] = response.json()
    return document


def test_openapi_json_is_valid_openapi_3(client: Client) -> None:
    document = _openapi_document(client)

    assert document["openapi"].startswith("3.")
    validate(document)


def test_openapi_info(client: Client) -> None:
    info = _openapi_document(client)["info"]

    assert info["title"] == "OPEN API"
    assert info["version"] == "0.1.0"
    assert info["description"].strip()


def test_docs_page_renders(client: Client) -> None:
    response = client.get("/api/v1/docs")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")


class _Item(Schema):
    k: int


class _Payload(Schema):
    items: list[_Item]


def _build_throwaway_api() -> OpenNinjaAPI:
    """An API with test-only operations, never mounted in the URLconf."""
    api = OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")

    @api.post("/items/{pid}", operation_id="create_item")
    def create_item(request: HttpRequest, pid: int, payload: _Payload) -> int:
        return pid

    @api.get("/conflict", operation_id="conflict")
    def conflict(request: HttpRequest) -> None:
        raise HttpError(409, "That already exists.")

    @api.get("/missing", operation_id="missing")
    def missing(request: HttpRequest) -> None:
        raise Http404("secret detail")

    return api


@pytest.fixture
def throwaway_api() -> OpenNinjaAPI:
    return _build_throwaway_api()


@pytest.fixture
def throwaway_client(throwaway_api: OpenNinjaAPI) -> TestClient:
    return TestClient(throwaway_api)


def _error_body(content: dict[str, Any]) -> ErrorResponse:
    assert set(content) == {"code", "message", "errors"}
    return ErrorResponse.model_validate(content)


def test_validation_error_shape(throwaway_client: TestClient) -> None:
    response = throwaway_client.post(
        "/items/abc", json={"items": [{"k": "not-a-number"}]}
    )

    assert response.status_code == 422
    body = _error_body(response.json())
    assert body.code == "validation_error"
    assert body.message == "The request is not valid."
    by_field = {error.field: error for error in body.errors}
    assert set(by_field) == {"path.pid", "body.items.0.k"}
    assert by_field["path.pid"].code == "int_parsing"
    assert by_field["body.items.0.k"].code == "int_parsing"
    assert by_field["body.items.0.k"].message


def test_validation_error_copies_no_input_or_ctx(throwaway_client: TestClient) -> None:
    """Items hold only field, code, and message, so the int_parsing value is absent.

    Pydantic's own message is kept, so this does not prove that no message can
    ever contain a submitted value: custom validators must not put it there.
    """
    secret = "do-not-echo-this-value"
    response = throwaway_client.post("/items/1", json={"items": [{"k": secret}]})

    assert response.status_code == 422
    errors = response.json()["errors"]
    assert [error["code"] for error in errors] == ["int_parsing"]
    for error in errors:
        assert set(error) == {"field", "code", "message"}
    assert secret not in response.content.decode()


def test_malformed_body_returns_error_schema(throwaway_client: TestClient) -> None:
    response = throwaway_client.post("/items/1", body=b"{not json")

    assert response.status_code == 400
    body = _error_body(response.json())
    assert body.code == "bad_request"
    assert body.message
    assert body.errors == []


def test_http_error_returns_error_schema(throwaway_client: TestClient) -> None:
    response = throwaway_client.get("/conflict")

    assert response.status_code == 409
    body = _error_body(response.json())
    assert body.code == "conflict"
    assert body.message == "That already exists."
    assert body.errors == []


@pytest.mark.parametrize("debug", [True, False])
def test_404_hides_detail(
    throwaway_client: TestClient, settings: Settings, debug: bool
) -> None:
    settings.DEBUG = debug

    response = throwaway_client.get("/missing")

    assert response.status_code == 404
    assert "secret detail" not in response.content.decode()
    body = _error_body(response.json())
    assert body.code == "not_found"
    assert body.message == "Not found."
    assert body.errors == []


ERROR_REF = "#/components/schemas/ErrorResponse"


def _throwaway_document(api: OpenNinjaAPI) -> dict[str, Any]:
    """The API's OpenAPI document as JSON would serve it (string status keys)."""
    schema = api.get_openapi_schema(path_prefix="")
    document: dict[str, Any] = json.loads(json.dumps(schema, cls=NinjaJSONEncoder))
    return document


def test_error_schema_documented(client: Client) -> None:
    schemas = _openapi_document(client)["components"]["schemas"]

    assert {"ErrorResponse", "FieldError"} <= set(schemas)
    assert set(schemas["ErrorResponse"]["properties"]) == {"code", "message", "errors"}


def test_422_documented_only_for_operations_with_input(
    throwaway_api: OpenNinjaAPI,
) -> None:
    document = _throwaway_document(throwaway_api)
    paths = document["paths"]

    with_input = paths["/items/{pid}"]["post"]["responses"]
    assert with_input["422"]["content"]["application/json"]["schema"] == {
        "$ref": ERROR_REF
    }
    assert "422" not in paths["/conflict"]["get"]["responses"]
    assert "422" not in paths["/missing"]["get"]["responses"]
    validate(document)


def test_422_body_matches_documented_schema(
    throwaway_api: OpenNinjaAPI, throwaway_client: TestClient
) -> None:
    document = _throwaway_document(throwaway_api)
    response = throwaway_client.post("/items/abc", json={"items": [{"k": "s"}]})

    assert response.status_code == 422
    body = response.json()
    assert {error["field"] for error in body["errors"]} == {
        "path.pid",
        "body.items.0.k",
    }
    # The root carries the document's components, so "#/components/schemas/..."
    # references inside ErrorResponse resolve against it.
    schema = {"$ref": ERROR_REF, "components": document["components"]}
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(body)


def _operations(target: OpenNinjaAPI) -> list[Operation]:
    """Every operation of every router mounted on the API, nested ones included."""
    return [
        operation
        for bound_router in target._get_bound_routers()
        for path_view in bound_router.path_operations.values()
        for operation in path_view.operations
    ]


def test_every_operation_has_explicit_operation_id() -> None:
    for operation in _operations(api):
        assert isinstance(operation.operation_id, str)
        assert operation.operation_id.strip(), operation.view_func.__name__


def test_operation_walk_is_not_vacuous(throwaway_api: OpenNinjaAPI) -> None:
    operation_ids = {operation.operation_id for operation in _operations(throwaway_api)}

    assert operation_ids == {"create_item", "conflict", "missing"}


def test_missing_operation_id_fails_schema_generation() -> None:
    no_id_api = OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")

    @no_id_api.get("/no-id")
    def view_without_operation_id(request: HttpRequest) -> None:
        return None

    with pytest.raises(ImproperlyConfigured, match="view_without_operation_id"):
        no_id_api.get_openapi_schema(path_prefix="")


def test_api_client_fixture(api_client: TestClient) -> None:
    response = api_client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "OPEN API"

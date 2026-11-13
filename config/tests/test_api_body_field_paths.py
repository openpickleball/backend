"""Validation error `field` paths name the wire location of body errors."""

import uuid
from typing import Any

import pytest
from django.http import HttpRequest
from ninja import Body, Schema
from ninja.testing import TestClient

from config.api import OpenNinjaAPI


class _A(Schema):
    x: int


class _B(Schema):
    x: int


class _Item(Schema):
    k: int


class _Payload(Schema):
    items: list[_Item]


@pytest.fixture
def body_client() -> TestClient:
    api = OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")

    @api.post("/two", operation_id="two_schemas")
    def two_schemas(request: HttpRequest, a: _A, b: _B) -> int:
        return 1

    @api.post("/scalars", operation_id="scalars")
    def scalars(
        request: HttpRequest, name: str = Body(...), age: int = Body(...)
    ) -> int:
        return 1

    @api.post("/one-scalar", operation_id="one_scalar")
    def one_scalar(request: HttpRequest, age: int = Body(...)) -> int:
        return 1

    @api.post("/single", operation_id="single_schema")
    def single_schema(request: HttpRequest, payload: _Payload) -> int:
        return 1

    return TestClient(api)


def _fields(client: TestClient, path: str, body: Any) -> list[str]:
    response = client.post(path, json=body)
    assert response.status_code == 422
    return [error["field"] for error in response.json()["errors"]]


def test_two_body_schemas_keep_parameter_names(body_client: TestClient) -> None:
    fields = _fields(body_client, "/two", {"a": {"x": "s"}, "b": {"x": "s"}})

    assert sorted(fields) == ["body.a.x", "body.b.x"]


def test_missing_second_body_schema_names_it(body_client: TestClient) -> None:
    assert _fields(body_client, "/two", {"a": {"x": 1}}) == ["body.b"]


def test_scalar_body_parameters_keep_their_names(body_client: TestClient) -> None:
    assert _fields(body_client, "/scalars", {"name": "n", "age": "zz"}) == ["body.age"]


def test_single_scalar_body_is_the_whole_body(body_client: TestClient) -> None:
    assert _fields(body_client, "/one-scalar", "zz") == ["body"]


def test_single_schema_body_drops_parameter_name(body_client: TestClient) -> None:
    fields = _fields(body_client, "/single", {"items": [{"k": "s"}]})

    assert fields == ["body.items.0.k"]

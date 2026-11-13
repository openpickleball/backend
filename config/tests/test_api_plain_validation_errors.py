"""A ValidationError with plain `{name: message}` items keeps every item."""

import uuid

import pytest
from django.http import HttpRequest
from ninja.errors import ValidationError
from ninja.testing import TestClient

from config.api import OpenNinjaAPI


@pytest.fixture
def plain_client() -> TestClient:
    api = OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")

    @api.get("/cursor", operation_id="bad_cursor")
    def bad_cursor(request: HttpRequest) -> None:
        # The shape ninja's own CursorPagination raises.
        raise ValidationError([{"cursor": "Invalid Cursor"}])

    @api.get("/two-keys", operation_id="two_keys")
    def two_keys(request: HttpRequest) -> None:
        raise ValidationError([{"start": "Too early", "end": "Too late"}])

    return TestClient(api)


def test_plain_item_becomes_field_error(plain_client: TestClient) -> None:
    response = plain_client.get("/cursor")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["errors"] == [
        {"field": "cursor", "code": "invalid", "message": "Invalid Cursor"}
    ]


def test_plain_item_gives_one_field_error_per_key(plain_client: TestClient) -> None:
    response = plain_client.get("/two-keys")

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"field": "start", "code": "invalid", "message": "Too early"},
        {"field": "end", "code": "invalid", "message": "Too late"},
    ]

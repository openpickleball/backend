"""A ``DuplicateMatchError`` raised in an operation returns 409 with an
``ErrorResponse`` that names the earlier match's OPEN code, in the message and
in the item's ``existing_code``, only when the submitter may know it."""

import uuid

import pytest
from django.http import HttpRequest
from django.test import Client
from ninja.testing import TestClient

from apps.matches.services import (
    DuplicateKind,
    DuplicateMatchError,
    MatchIssue,
    MatchValidationError,
)
from config.api import OpenNinjaAPI
from config.api_errors import ErrorResponse

EXISTING_CODE = "M-0000-0000-0000"


def _client(kind: DuplicateKind, code: str | None = EXISTING_CODE) -> TestClient:
    """An API with one test-only operation, never mounted in the URLconf."""
    api = OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")

    @api.post("/matches", operation_id="submit_duplicate")
    def submit_duplicate(request: HttpRequest) -> None:
        raise DuplicateMatchError(kind, code)

    return TestClient(api)


def test_duplicate_match_error_without_a_code_returns_fixed_text() -> None:
    message = "This match was already submitted."

    response = _client(DuplicateKind.DUPLICATE_MATCH, None).post("/matches")

    assert response.status_code == 409
    body = ErrorResponse.model_validate(response.json())
    assert body.code == "duplicate_match"
    assert body.message == message
    # No code: the item has no `existing_code` key at all.
    assert response.json()["errors"] == [
        {"field": "match", "code": "duplicate_match", "message": message}
    ]
    assert "M-" not in response.content.decode()


@pytest.mark.parametrize(
    ("kind", "field", "message"),
    [
        (
            DuplicateKind.DUPLICATE_MATCH,
            "match",
            f"This match was already submitted as {EXISTING_CODE}.",
        ),
        (
            DuplicateKind.DUPLICATE_EXTERNAL_ID,
            "external_id",
            "This application already submitted a match with this external ID: "
            f"{EXISTING_CODE}.",
        ),
    ],
)
def test_duplicate_match_error_returns_409(
    kind: DuplicateKind, field: str, message: str
) -> None:
    response = _client(kind).post("/matches")

    assert response.status_code == 409
    content = response.json()
    assert set(content) == {"code", "message", "errors"}
    body = ErrorResponse.model_validate(content)
    assert body.code == "duplicate_match"
    assert body.message == message
    assert content["errors"] == [
        {
            "field": field,
            "code": kind.value,
            "message": message,
            "existing_code": EXISTING_CODE,
        }
    ]


def test_other_error_items_have_no_existing_code() -> None:
    api = OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")

    @api.post("/matches", operation_id="submit_invalid")
    def submit_invalid(request: HttpRequest) -> None:
        raise MatchValidationError(
            [MatchIssue(field="games", code="too_few", message="Too few games.")]
        )

    response = TestClient(api).post("/matches")

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"field": "games", "code": "too_few", "message": "Too few games."}
    ]


def test_field_error_documents_optional_existing_code(client: Client) -> None:
    document = client.get("/api/v1/openapi.json").json()
    field_error = document["components"]["schemas"]["FieldError"]

    prop = field_error["properties"]["existing_code"]
    assert {"type": "string"} in prop["anyOf"]
    assert {"type": "null"} in prop["anyOf"]
    assert "existing_code" not in field_error["required"]

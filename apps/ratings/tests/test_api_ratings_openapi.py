"""The rating operations in the OpenAPI document (issue #53).

Both operations have explicit operation ids and document their 200,
404, 422, and 429 answers; the history operation documents its 401 too,
and the public lookup needs no sign-in. Every rating schema requires a
non-null reliability next to the rating and a ``demonstration`` that is
always true, and the descriptions say why. Made-up data only.
"""

from typing import Any

import pytest
from django.test import Client

LOOKUP = "/api/v1/players/{code}/ratings"
HISTORY = "/api/v1/players/{code}/rating-history"
ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}
RATING_FORMATS = ["singles", "doubles", "mixed_doubles", "doubles_men", "doubles_women"]
RELIABILITY_NOTE = "requires any app that shows an OPEN rating to show its reliability"


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    found: dict[str, Any] = Client().get("/api/v1/openapi.json").json()
    return found


def _schema(document: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = document["components"]["schemas"][name]
    return found


def _statuses(operation: dict[str, Any]) -> set[str]:
    return {str(code) for code in operation["responses"]}


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_lookup_is_documented_without_sign_in(document: dict[str, Any]) -> None:
    operation = document["paths"][LOOKUP]["get"]

    assert operation["operationId"] == "get_player_ratings"
    assert _statuses(operation) == {"200", "404", "422", "429"}
    assert not operation.get("security")
    for status in ("404", "422", "429"):
        content = operation["responses"][status]["content"]["application/json"]
        assert content["schema"] == ERROR_REF, status
    assert "Retry-After" in operation["responses"]["429"]["headers"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PlayerRatingsOut"
    }


def test_the_history_is_documented_with_sign_in(document: dict[str, Any]) -> None:
    operation = document["paths"][HISTORY]["get"]

    assert operation["operationId"] == "list_player_rating_history"
    assert _statuses(operation) == {"200", "401", "404", "422", "429"}
    assert operation.get("security")
    for status in ("401", "404", "422", "429"):
        content = operation["responses"][status]["content"]["application/json"]
        assert content["schema"] == ERROR_REF, status


def test_the_history_documents_its_paging_bounds(document: dict[str, Any]) -> None:
    parameters = {
        parameter["name"]: parameter["schema"]
        for parameter in document["paths"][HISTORY]["get"]["parameters"]
    }

    assert parameters["limit"]["minimum"] == 1
    assert parameters["limit"]["maximum"] == 100
    assert parameters["limit"]["default"] == 20
    assert parameters["offset"]["minimum"] == 0
    assert parameters["offset"]["maximum"] == 2**63 - 1
    paged = _schema(document, "PagedRatingHistoryItemOut")
    assert set(paged["required"]) == {"items", "count"}
    assert paged["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/RatingHistoryItemOut"
    }


@pytest.mark.parametrize("name", ["RatingOut", "RatingHistoryItemOut"])
def test_reliability_is_required_and_never_null(
    document: dict[str, Any], name: str
) -> None:
    schema = _schema(document, name)

    assert {"rating", "reliability", "imported_reliability"} <= set(schema["required"])
    assert schema["properties"]["reliability"]["type"] == "number"
    assert schema["properties"]["rating"]["type"] == "number"
    assert "anyOf" not in schema["properties"]["reliability"]
    assert RELIABILITY_NOTE in _flat(schema["properties"]["reliability"]["description"])
    assert RELIABILITY_NOTE in _flat(schema["description"])


@pytest.mark.parametrize(
    "name", ["RatingOut", "RatingHistoryItemOut", "PlayerRatingsOut"]
)
def test_demonstration_is_required_and_always_true(
    document: dict[str, Any], name: str
) -> None:
    schema = _schema(document, name)

    assert "demonstration" in schema["required"]
    assert schema["properties"]["demonstration"]["const"] is True
    assert "demonstration package" in _flat(schema["description"])


def test_the_ratings_object_is_keyed_by_rating_format(
    document: dict[str, Any],
) -> None:
    ratings = _schema(document, "PlayerRatingsOut")["properties"]["ratings"]

    assert ratings["type"] == "object"
    assert ratings["additionalProperties"] == {"$ref": "#/components/schemas/RatingOut"}
    assert ratings["propertyNames"] == {"$ref": "#/components/schemas/RatingFormat"}
    assert _schema(document, "RatingFormat")["enum"] == RATING_FORMATS


def test_history_kinds_are_documented(document: dict[str, Any]) -> None:
    assert _schema(document, "RatingChangeKind")["enum"] == [
        "live_update",
        "replay",
        "removed",
    ]


@pytest.mark.parametrize("path", [LOOKUP, HISTORY])
def test_operation_descriptions_name_reliability_and_demonstration(
    document: dict[str, Any], path: str
) -> None:
    description = _flat(document["paths"][path]["get"]["description"])

    assert RELIABILITY_NOTE in description
    assert "demonstration rating" in description


def test_no_rating_schema_holds_an_id_a_delta_or_a_match(
    document: dict[str, Any],
) -> None:
    for name in ("RatingOut", "RatingHistoryItemOut", "PlayerRatingsOut"):
        fields = set(_schema(document, name)["properties"])
        assert not fields & {"id", "delta", "match", "match_code", "replay"}, name
        assert not fields & {"display_name", "gender_division", "rd"}, name

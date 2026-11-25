"""Throttles on the real OPEN API (issue #34, ADR 0030).

Every operation that needs sign-in shares one per-Account throttle,
except player search, which keeps only its own ``SearchThrottle`` (ADR
0018), and reporting an Organizer, which keeps only its own
``OrganizerReportThrottle`` (#271). No public operation is mounted yet; one would need no sign-in and
have only a client IP throttle. The health checks are never throttled.
A refused request gets a 429 ``ErrorResponse`` with code ``throttled``
and a ``Retry-After`` header, and the OpenAPI document says so on every
throttled operation.
Made-up data only.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.test import Client, override_settings
from ninja import Schema
from ninja.operation import Operation
from openapi_spec_validator import validate

from apps.accounts.tests.factories import UserFactory
from apps.clubs.api import OrganizerReportThrottle
from apps.players.api import SearchThrottle
from config.api import OpenNinjaAPI, api
from config.auth import account_auth
from config.throttling import AccountThrottle, ClientIPThrottle, account_throttle

ME = "/api/v1/accounts/me"
AWAITING = "/api/v1/me/matches/awaiting-confirmation"
SEARCH = "/api/v1/players/search"
LIVE = "/api/v1/health/live"
ERROR_REF = "#/components/schemas/ErrorResponse"
HEALTH_OPERATIONS = {"health_live", "health_ready"}
# Operations anyone may call without sign-in, throttled per client IP.
# None is mounted yet; the client IP throttle is kept for a later one.
PUBLIC_OPERATIONS: set[str] = set()
# Signed-in operations with their own throttle instead of the Account one.
OWN_THROTTLE_OPERATIONS = {"search_players", "report_organizer"}
THROTTLED = {"code": "throttled", "message": "Too many requests.", "errors": []}


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def _operations(target: OpenNinjaAPI) -> list[Operation]:
    """Every operation of every router mounted on the API."""
    return [
        operation
        for bound_router in target._get_bound_routers()
        for path_view in bound_router.path_operations.values()
        for operation in path_view.operations
    ]


def _by_id() -> dict[str, Operation]:
    return {str(operation.operation_id): operation for operation in _operations(api)}


def _signed_in() -> Client:
    client = Client()
    client.force_login(UserFactory.create())
    return client


# Which operations have which throttles


def test_operation_walk_covers_the_api() -> None:
    operations = _by_id()

    assert {"get_current_account", "search_players", "health_live"} <= set(operations)
    assert len(operations) == len(_operations(api))


def test_every_signed_in_operation_shares_the_account_throttle() -> None:
    for operation_id, operation in _by_id().items():
        if (
            operation_id
            in HEALTH_OPERATIONS | PUBLIC_OPERATIONS | OWN_THROTTLE_OPERATIONS
        ):
            continue
        assert operation.auth_callbacks == account_auth, operation_id
        assert operation.throttle_objects == [account_throttle], operation_id
        assert operation.throttle_objects[0] is account_throttle, operation_id


def test_search_has_only_its_own_throttle() -> None:
    operation = _by_id()["search_players"]

    assert len(operation.throttle_objects) == 1
    assert isinstance(operation.throttle_objects[0], SearchThrottle)
    assert not isinstance(operation.throttle_objects[0], AccountThrottle)


def test_organizer_report_has_only_its_own_throttle() -> None:
    operation = _by_id()["report_organizer"]

    assert operation.auth_callbacks == account_auth
    assert len(operation.throttle_objects) == 1
    assert isinstance(operation.throttle_objects[0], OrganizerReportThrottle)
    assert not isinstance(operation.throttle_objects[0], AccountThrottle)


def test_operation_throttle_replaces_the_router_throttle() -> None:
    (players_router,) = [
        bound_router
        for bound_router in api._get_bound_routers()
        if "/search" in bound_router.path_operations
    ]

    assert players_router.throttle is account_throttle
    (search,) = players_router.path_operations["/search"].operations
    assert account_throttle not in search.throttle_objects


def test_health_checks_are_never_throttled() -> None:
    operations = _by_id()

    for operation_id in HEALTH_OPERATIONS:
        assert operations[operation_id].throttle_objects == [], operation_id


# Name kept from #34: it also expects any public operation (none yet).
def test_every_operation_without_sign_in_is_a_health_check() -> None:
    unauthenticated = {
        operation_id
        for operation_id, operation in _by_id().items()
        if not operation.auth_callbacks
    }

    assert unauthenticated == HEALTH_OPERATIONS | PUBLIC_OPERATIONS


def test_every_public_operation_has_only_a_client_ip_throttle() -> None:
    operations = _by_id()

    for operation_id in PUBLIC_OPERATIONS:
        operation = operations[operation_id]
        assert not operation.auth_callbacks, operation_id
        assert len(operation.throttle_objects) == 1, operation_id
        assert isinstance(operation.throttle_objects[0], ClientIPThrottle), operation_id


# Behavior through Django


@pytest.mark.django_db
@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_past_the_limit_is_a_throttled_error_with_retry_after() -> None:
    client = _signed_in()
    assert [client.get(ME).status_code for _ in range(2)] == [200, 200]

    response = client.get(ME)

    assert response.status_code == 429
    assert response.json() == THROTTLED
    retry_after = response["Retry-After"]
    assert retry_after.isdigit()
    assert 1 <= int(retry_after) <= 60


@pytest.mark.django_db
@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1, API_ACCOUNT_THROTTLE_PER_DAY=1)
def test_day_refusal_retry_after_is_until_the_day_ends() -> None:
    client = _signed_in()
    assert client.get(ME).status_code == 200

    response = client.get(ME)

    assert response.status_code == 429
    assert 1 <= int(response["Retry-After"]) <= 86_400


@pytest.mark.django_db
@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_operations_share_one_account_budget() -> None:
    client = _signed_in()
    assert client.get(ME).status_code == 200
    assert client.get(AWAITING).status_code == 200

    assert client.get(ME).status_code == 429
    assert client.get(AWAITING).status_code == 429


@pytest.mark.django_db
@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_accounts_have_separate_budgets() -> None:
    first = _signed_in()
    for _ in range(2):
        first.get(ME)
    assert first.get(ME).status_code == 429

    assert _signed_in().get(ME).status_code == 200


@pytest.mark.django_db
@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_search_does_not_use_the_account_budget() -> None:
    client = _signed_in()
    for _ in range(5):
        assert client.get(SEARCH, {"q": "sam"}).status_code == 200

    assert [client.get(ME).status_code for _ in range(3)] == [200, 200, 429]
    assert client.get(SEARCH, {"q": "sam"}).status_code == 200


@pytest.mark.django_db
@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1, API_ACCOUNT_THROTTLE_PER_DAY=1)
def test_unauthenticated_requests_are_not_counted() -> None:
    assert [Client().get(ME).status_code for _ in range(3)] == [401] * 3
    assert not cache._cache  # type: ignore[attr-defined]


@pytest.mark.django_db
@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1, API_ACCOUNT_THROTTLE_PER_DAY=1)
def test_health_is_never_throttled() -> None:
    for _ in range(20):
        assert Client().get(LIVE).status_code == 200
    client = _signed_in()
    for _ in range(5):
        assert client.get(LIVE).status_code == 200


@pytest.mark.django_db
def test_a_changed_rate_applies_on_the_next_request() -> None:
    client = _signed_in()
    with override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1):
        assert client.get(ME).status_code == 200
        assert client.get(ME).status_code == 429

    with override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=3):
        assert client.get(ME).status_code == 200


# The OpenAPI document


def _document() -> dict[str, Any]:
    document: dict[str, Any] = api.get_openapi_schema(path_prefix="/api/v1/")
    return document


def _document_operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        operation["operationId"]: operation
        for path_item in document["paths"].values()
        for operation in path_item.values()
    }


def _response(operation: dict[str, Any], status: int) -> dict[str, Any] | None:
    for code, body in operation["responses"].items():
        if str(code) == str(status):
            found: dict[str, Any] = body
            return found
    return None


def test_every_throttled_operation_documents_429_with_retry_after() -> None:
    document = _document()
    documented = _document_operations(document)
    throttled = {
        operation_id
        for operation_id, operation in _by_id().items()
        if operation.throttle_objects
    }
    assert throttled

    for operation_id in throttled:
        response = _response(documented[operation_id], 429)
        assert response is not None, operation_id
        assert response["content"]["application/json"]["schema"] == {
            "$ref": ERROR_REF
        }, operation_id
        header = response["headers"]["Retry-After"]
        assert header["schema"]["type"] == "integer", operation_id


def test_unthrottled_operations_document_no_429() -> None:
    documented = _document_operations(_document())

    for operation_id in HEALTH_OPERATIONS:
        assert _response(documented[operation_id], 429) is None, operation_id


def test_document_with_429_is_valid_openapi() -> None:
    validate(Client().get("/api/v1/openapi.json").json())


class _Other(Schema):
    detail: str


def test_own_429_schema_on_a_throttled_operation_fails_the_document() -> None:
    throwaway = OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")

    @throwaway.get(
        "/limited",
        response={200: None, 429: _Other},
        throttle=ClientIPThrottle("own_429"),
        operation_id="own_429",
    )
    def limited(request: HttpRequest) -> None:
        return None

    with pytest.raises(ImproperlyConfigured, match="own_429"):
        throwaway.get_openapi_schema(path_prefix="")


def test_unthrottled_operation_gets_no_429() -> None:
    throwaway = OpenNinjaAPI(urls_namespace=f"test-{uuid.uuid4().hex}")

    @throwaway.get("/free", response={200: None}, operation_id="free")
    def free(request: HttpRequest) -> None:
        return None

    document = throwaway.get_openapi_schema(path_prefix="")

    assert _response(_document_operations(document)["free"], 429) is None

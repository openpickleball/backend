"""Tests for the liveness and readiness health checks (issue #6)."""

import logging
from collections.abc import Callable
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache, caches
from django.db import OperationalError, connections
from django.test import Client
from pytest_django.fixtures import Settings

LIVE_URL = "/api/v1/health/live"
READY_URL = "/api/v1/health/ready"

DB_ERROR = 'connection to server at "db.internal" (10.0.0.5) failed'
CACHE_ERROR = "Error connecting to cache.internal:6379"


def _raise(exc: Exception) -> Callable[..., Any]:
    def raiser(*args: Any, **kwargs: Any) -> Any:
        raise exc

    return raiser


@pytest.fixture
def database_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the default connection fail to open a cursor."""
    monkeypatch.setattr(
        connections["default"], "cursor", _raise(OperationalError(DB_ERROR))
    )


@pytest.fixture
def cache_down() -> Any:
    """Make every write to the default cache fail."""
    with mock.patch(
        "config.health.cache.set", side_effect=ConnectionError(CACHE_ERROR)
    ) as patched:
        yield patched


def test_live_returns_ok(client: Client) -> None:
    # No db mark: any database access fails the test under pytest-django.
    with (
        mock.patch.object(cache, "get", side_effect=AssertionError("cache used")),
        mock.patch.object(cache, "set", side_effect=AssertionError("cache used")),
    ):
        response = client.get(LIVE_URL)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_live_ok_when_database_down(client: Client, database_down: None) -> None:
    response = client.get(LIVE_URL)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_ready_ok(client: Client) -> None:
    response = client.get(READY_URL)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "cache": "ok"},
    }


@pytest.mark.django_db
def test_ready_503_when_database_down(client: Client, database_down: None) -> None:
    response = client.get(READY_URL)

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "fail", "cache": "ok"},
    }
    body = response.content.decode()
    for secret in ("db.internal", "10.0.0.5", "OperationalError"):
        assert secret not in body


@pytest.mark.django_db
def test_ready_503_when_cache_down(client: Client, cache_down: Any) -> None:
    response = client.get(READY_URL)

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "ok", "cache": "fail"},
    }
    body = response.content.decode()
    for secret in ("cache.internal", "6379", "ConnectionError"):
        assert secret not in body


@pytest.mark.django_db
def test_ready_503_when_cache_stores_nothing(
    client: Client, settings: Settings
) -> None:
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}
    }

    response = client.get(READY_URL)

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "ok", "cache": "fail"},
    }


@pytest.mark.django_db
def test_ready_503_when_both_down(
    client: Client, database_down: None, cache_down: Any
) -> None:
    response = client.get(READY_URL)

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "fail", "cache": "fail"},
    }


@pytest.mark.django_db
def test_ready_cache_key_is_removed(client: Client) -> None:
    response = client.get(READY_URL)

    assert response.status_code == 200
    store = caches["default"]._cache  # type: ignore[attr-defined]
    assert not [key for key in store if "health:ready:" in key]


@pytest.mark.django_db
def test_responses_have_only_status_fields(client: Client) -> None:
    live = client.get(LIVE_URL).json()
    ready = client.get(READY_URL).json()

    assert set(live) == {"status"}
    assert set(ready) == {"status", "checks"}
    assert set(ready["checks"]) == {"database", "cache"}


@pytest.mark.django_db
def test_no_auth_required() -> None:
    anonymous = Client()

    assert anonymous.get(LIVE_URL).status_code == 200
    assert anonymous.get(READY_URL).status_code == 200


@pytest.mark.django_db
def test_never_cached(client: Client) -> None:
    for url in (LIVE_URL, READY_URL):
        response = client.get(url)

        assert "no-store" in response["Cache-Control"]


@pytest.mark.django_db
def test_never_cached_when_not_ready(client: Client, database_down: None) -> None:
    response = client.get(READY_URL)

    assert response.status_code == 503
    assert "no-store" in response["Cache-Control"]


def test_openapi_documents_health(client: Client) -> None:
    document = client.get("/api/v1/openapi.json").json()
    paths = document["paths"]

    live = paths["/api/v1/health/live"]["get"]
    ready = paths["/api/v1/health/ready"]["get"]

    assert live["operationId"] == "health_live"
    assert ready["operationId"] == "health_ready"
    assert live["tags"] == ["health"]
    assert ready["tags"] == ["health"]
    assert set(live["responses"]) == {"200"}
    assert set(ready["responses"]) == {"200", "503"}

    def schema_of(operation: dict[str, Any], code: str) -> Any:
        return operation["responses"][code]["content"]["application/json"]["schema"]

    assert schema_of(ready, "200") == schema_of(ready, "503")


@pytest.mark.django_db
def test_failure_logged_without_detail(
    client: Client, database_down: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="config.health"):
        client.get(READY_URL)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    messages = [r.getMessage() for r in warnings]
    assert any("database" in m and "OperationalError" in m for m in messages)
    assert all("db.internal" not in m for m in messages)
    assert "db.internal" not in caplog.text

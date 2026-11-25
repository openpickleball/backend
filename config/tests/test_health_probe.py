"""Tests for HealthProbeMiddleware (issue #61, ADR 0039).

App Platform probes the container over plain http with a Host header
that ALLOWED_HOSTS refuses. The middleware answers a GET of exactly the
two health paths itself in that case. Every other request keeps Django's
host check and https redirect.
"""

import logging
from collections.abc import Iterator

import pytest
from django.conf import settings
from django.db import OperationalError, connections
from django.test import Client, override_settings
from django.urls import resolve, reverse

from config import health
from config.health_probe import PROBE_PATHS

LIVE_URL = "/api/v1/health/live"
READY_URL = "/api/v1/health/ready"

# Made-up hosts. The instance address looks like the ones App Platform uses.
INSTANCE_HOST = "10.244.0.5"
ALLOWED_HOST = "api.example.org"
ALLOWED_ORIGIN = "https://app.example.org"

MIDDLEWARE_PATH = "config.health_probe.HealthProbeMiddleware"


@pytest.fixture
def deployed() -> Iterator[None]:
    """The DEBUG-off security settings a deploy runs with."""
    with override_settings(
        ALLOWED_HOSTS=[ALLOWED_HOST],
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SECURE_HSTS_SECONDS=31536000,
        CORS_ALLOWED_ORIGINS=[ALLOWED_ORIGIN],
    ):
        yield


def test_probe_middleware_comes_first() -> None:
    assert settings.MIDDLEWARE[0] == MIDDLEWARE_PATH


def test_probe_paths_are_the_health_operations() -> None:
    assert set(PROBE_PATHS) == {LIVE_URL, READY_URL}
    assert reverse("api-v1:live") == LIVE_URL
    assert reverse("api-v1:ready") == READY_URL
    assert resolve(LIVE_URL).url_name == health.live.__name__
    assert resolve(READY_URL).url_name == health.ready.__name__


def test_live_with_the_instance_ip_gets_200(deployed: None) -> None:
    response = Client().get(LIVE_URL, HTTP_HOST=INSTANCE_HOST)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "no-store" in response["Cache-Control"]


@pytest.mark.django_db
def test_ready_with_an_empty_host_gets_200(deployed: None) -> None:
    response = Client().get(READY_URL, HTTP_HOST="")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "cache": "ok"},
    }
    assert "no-store" in response["Cache-Control"]


def test_live_with_no_host_header_gets_200(deployed: None) -> None:
    # The client falls back to SERVER_NAME, which no allowed host matches.
    response = Client(SERVER_NAME="0.0.0.0").get(LIVE_URL)

    assert response.status_code == 200


@pytest.mark.django_db
def test_ready_with_a_bad_host_reports_the_database_down(
    deployed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> None:
        raise OperationalError("connection to db.internal failed")

    monkeypatch.setattr(connections["default"], "cursor", fail)
    response = Client().get(READY_URL, HTTP_HOST=INSTANCE_HOST)

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "fail"


def test_live_over_plain_http_with_an_allowed_host_is_answered(deployed: None) -> None:
    response = Client().get(LIVE_URL, HTTP_HOST=ALLOWED_HOST)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/api/v1/accounts/me", "/admin/login/", "/"])
def test_a_bad_host_on_another_path_still_gets_400(deployed: None, path: str) -> None:
    response = Client().get(path, HTTP_HOST=INSTANCE_HOST)

    assert response.status_code == 400


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/health/ready/",
        "/api/v1/health/livez",
        "/api/v1/health/live/x",
        "/api/v1/health/",
        "/api/v1/health/ready%2F",
    ],
)
def test_a_near_miss_path_with_a_bad_host_gets_400(deployed: None, path: str) -> None:
    response = Client().get(path, HTTP_HOST=INSTANCE_HOST)

    assert response.status_code == 400


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "head"])
def test_another_method_with_a_bad_host_gets_400(deployed: None, method: str) -> None:
    response = getattr(Client(), method)(READY_URL, HTTP_HOST=INSTANCE_HOST)

    assert response.status_code == 400


def test_another_method_over_plain_http_is_redirected(deployed: None) -> None:
    response = Client().post(LIVE_URL, HTTP_HOST=ALLOWED_HOST)

    assert response.status_code == 301


def test_another_path_over_plain_http_is_redirected(deployed: None) -> None:
    response = Client().get("/api/v1/accounts/me", HTTP_HOST=ALLOWED_HOST)

    assert response.status_code == 301
    assert response["Location"] == f"https://{ALLOWED_HOST}/api/v1/accounts/me"


def test_an_allowed_host_over_https_goes_through_the_normal_chain(
    deployed: None,
) -> None:
    response = Client().get(
        LIVE_URL,
        HTTP_HOST=ALLOWED_HOST,
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_ORIGIN=ALLOWED_ORIGIN,
    )

    assert response.status_code == 200
    assert response["Strict-Transport-Security"].startswith("max-age=31536000")
    assert response["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN


def test_a_probe_answer_sets_no_cookie_and_varies_on_nothing(deployed: None) -> None:
    response = Client().get(LIVE_URL, HTTP_HOST=INSTANCE_HOST)

    assert response.status_code == 200
    assert not response.cookies
    assert "Cookie" not in response.get("Vary", "")


def test_a_probe_answer_never_logs_the_host(
    deployed: None, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "probe-marker-k2v.example.org"
    with caplog.at_level(logging.DEBUG):
        response = Client().get(LIVE_URL, HTTP_HOST=marker)

    assert response.status_code == 200
    assert marker not in caplog.text

"""Account authentication on the OPEN API (issue #30, AC-B).

`GET /api/v1/accounts/me` takes allauth's app client token (the
`X-Session-Token` header) or the browser client's session cookie. A
request with neither, or with a token that does not authenticate, gets a
401 `ErrorResponse`. Made-up addresses only.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import Client
from openapi_spec_validator import validate

from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    app_headers,
    browser_client,
    csrf_header,
    login,
    signup,
    verify_from_last_mail,
)

# Queued account email is sent when each request returns (ADR 0016).
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

ME = "/api/v1/accounts/me"
EMAIL = "me.player@example.org"
ERROR_REF = "#/components/schemas/ErrorResponse"
UNAUTHORIZED = {
    "code": "unauthorized",
    "message": "Authentication required.",
    "errors": [],
}


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    """allauth keeps its rate limits in the cache: start each test empty."""
    cache.clear()
    yield
    cache.clear()


def _verified() -> None:
    signup(Client(), APP, EMAIL)
    verify_from_last_mail(Client(), APP)


def _app_token() -> str:
    _verified()
    token: str = login(Client(), APP, EMAIL).json()["meta"]["session_token"]
    return token


def test_app_client_token_reads_its_account() -> None:
    token = _app_token()

    response = Client().get(ME, headers=app_headers(token))

    assert response.status_code == 200
    assert response.json() == {"email": EMAIL}


def test_browser_client_session_reads_its_account() -> None:
    _verified()
    client = browser_client()
    assert login(client, BROWSER, EMAIL, **csrf_header(client)).status_code == 200

    response = client.get(ME)

    assert response.status_code == 200
    assert response.json() == {"email": EMAIL}


def test_no_credentials_get_401_error_response() -> None:
    response = Client().get(ME)

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


def test_unknown_token_gets_401() -> None:
    response = Client().get(ME, headers=app_headers("not-a-real-session-token"))

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


def test_pending_login_token_gets_401() -> None:
    # AC-A: before the address is verified, login leaves a pending flow and
    # its token does not authenticate.
    signup(Client(), APP, EMAIL)
    mail.outbox.clear()
    pending = login(Client(), APP, EMAIL).json()["meta"]["session_token"]

    response = Client().get(ME, headers=app_headers(pending))

    assert response.status_code == 401
    # The signup mail just went out, so the login sends none (allauth sends
    # one verification mail per address every 3 minutes).
    assert mail.outbox == []


def test_token_after_logout_gets_401() -> None:
    token = _app_token()
    Client().delete(f"{APP}/auth/session", headers=app_headers(token))

    response = Client().get(ME, headers=app_headers(token))

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


def test_browser_session_after_logout_gets_401() -> None:
    _verified()
    client = browser_client()
    login(client, BROWSER, EMAIL, **csrf_header(client))
    client.delete(f"{BROWSER}/auth/session", headers=csrf_header(client))

    assert client.get(ME).status_code == 401


def test_health_checks_stay_public() -> None:
    assert Client().get("/api/v1/health/live").status_code == 200


# The OpenAPI document.


def _document() -> dict[str, Any]:
    response = Client().get("/api/v1/openapi.json")
    assert response.status_code == 200
    document: dict[str, Any] = response.json()
    return document


def test_me_operation_is_documented_with_both_schemes_and_401() -> None:
    document = _document()
    operation = document["paths"][ME]["get"]

    assert operation["operationId"] == "get_current_account"
    schemes = document["components"]["securitySchemes"]
    assert {
        name: (scheme["type"], scheme["in"], scheme["name"])
        for name, scheme in schemes.items()
    } == {
        "SessionTokenAuth": ("apiKey", "header", "X-Session-Token"),
        "SessionAuth": ("apiKey", "cookie", settings.SESSION_COOKIE_NAME),
    }
    assert {name for item in operation["security"] for name in item} == set(schemes)
    unauthorized = operation["responses"]["401"]
    assert unauthorized["content"]["application/json"]["schema"] == {"$ref": ERROR_REF}
    assert set(operation["responses"]["200"]["content"]["application/json"]) == {
        "schema"
    }
    validate(document)


def test_public_operations_document_no_401() -> None:
    operation = _document()["paths"]["/api/v1/health/live"]["get"]

    assert "security" not in operation
    assert "401" not in operation["responses"]


def test_api_description_links_the_account_api() -> None:
    assert "/_allauth/openapi.html" in _document()["info"]["description"]

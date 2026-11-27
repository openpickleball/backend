"""allauth's OpenAPI document, with OPEN's account security changes (#100).

The session list and end use opaque string ids. Login and the MFA login
stage document the app client's ``X-Device-Token`` header, sent and
returned, and login documents its 429. The password rules list the
breached password code.
"""

from typing import Any

import pytest

from apps.accounts.openapi import open_schema

LOGIN = "/_allauth/{client}/v1/auth/login"
MFA_LOGIN = "/_allauth/{client}/v1/auth/2fa/authenticate"
PASSWORD_CHECK = "/_allauth/{client}/v1/auth/signup/password-check"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return open_schema()


def test_sessions_are_listed_and_ended_by_string_ids(spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]

    assert schemas["Session"]["properties"]["id"]["type"] == "string"
    assert schemas["EndSessions"]["properties"]["sessions"]["items"]["type"] == "string"


@pytest.mark.parametrize("path", [LOGIN, MFA_LOGIN])
def test_the_device_token_is_documented(spec: dict[str, Any], path: str) -> None:
    operation = spec["paths"][path]["post"]

    [header] = [p for p in operation["parameters"] if p.get("name") == "X-Device-Token"]
    assert header["in"] == "header"
    assert header["required"] is False
    assert "X-Device-Token" in operation["responses"]["200"]["headers"]


@pytest.mark.parametrize(
    "path",
    [
        LOGIN,
        MFA_LOGIN,
        "/_allauth/{client}/v1/auth/reauthenticate",
        "/_allauth/{client}/v1/auth/2fa/reauthenticate",
        "/_allauth/{client}/v1/auth/webauthn/authenticate",
        "/_allauth/{client}/v1/auth/webauthn/reauthenticate",
    ],
)
def test_the_lockout_429_is_documented(spec: dict[str, Any], path: str) -> None:
    responses = spec["paths"][path]["post"]["responses"]

    assert responses["429"] == {"$ref": "#/components/responses/TooManyRequests"}


def test_the_password_check_lists_the_breached_code(spec: dict[str, Any]) -> None:
    assert "password_breached" in spec["paths"][PASSWORD_CHECK]["post"]["description"]

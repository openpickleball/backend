"""allauth's OpenAPI document gives passkey login and the passkey MFA
login stage the app client's ``X-Device-Token`` header, sent and
returned, as it does the password login and the code MFA stage: every
completed login reads and writes it (``apps.accounts.devices``, issue
#117)."""

from typing import Any

import pytest

from apps.accounts.openapi import open_schema


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return open_schema()


@pytest.mark.parametrize(
    "path",
    [
        "/_allauth/{client}/v1/auth/webauthn/login",
        "/_allauth/{client}/v1/auth/webauthn/authenticate",
    ],
)
def test_the_device_token_is_documented_for_passkeys(
    spec: dict[str, Any], path: str
) -> None:
    operation = spec["paths"][path]["post"]

    [header] = [
        p for p in operation.get("parameters", []) if p.get("name") == "X-Device-Token"
    ]
    assert header["in"] == "header"
    assert header["required"] is False
    assert "X-Device-Token" in operation["responses"]["200"].get("headers", {})

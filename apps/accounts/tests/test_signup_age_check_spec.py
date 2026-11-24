"""allauth's OpenAPI document lists the signup age check (#223, ADR 0020).

With one headless client on, allauth pins the client into its paths and
drops the ``Client`` parameter; the age check follows it.
"""

from typing import Any

from django.test import override_settings

from apps.accounts.openapi import open_schema


def _operation(spec: dict[str, Any], path: str) -> dict[str, Any]:
    operation: dict[str, Any] = spec["paths"][path]["post"]
    return operation


@override_settings(HEADLESS_CLIENTS=("app",))
def test_one_client_pins_the_path_like_allauths() -> None:
    spec = open_schema()

    operation = _operation(spec, "/_allauth/app/v1/auth/signup/age-check")

    assert "parameters" not in operation
    assert "/_allauth/app/v1/auth/signup" in spec["paths"]
    assert not any("{client}" in path for path in spec["paths"])


def test_both_clients_keep_the_client_parameter() -> None:
    spec = open_schema()

    operation = _operation(spec, "/_allauth/{client}/v1/auth/signup/age-check")

    assert operation["parameters"] == [{"$ref": "#/components/parameters/Client"}]

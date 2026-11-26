"""What allauth's headless API exposes (issue #30).

The user object carries no row id (CLAUDE.md, Public identifiers), social
login is off (AC-C), and allauth's own OpenAPI document is served.
"""

import pytest
from django.conf import settings
from django.test import Client

from apps.accounts.headless_adapter import HeadlessAdapter
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    browser_client,
    csrf_header,
    login,
    post,
    signup,
    verify_from_last_mail,
)

pytestmark = pytest.mark.django_db

EMAIL = "payload.player@example.org"


def test_login_user_payload_has_no_row_id() -> None:
    signup(Client(), APP, EMAIL)
    verify_from_last_mail(Client(), APP)

    user = login(Client(), APP, EMAIL).json()["data"]["user"]

    assert "id" not in user
    assert "username" not in user
    assert user["email"] == EMAIL
    assert user["has_usable_password"] is True


def test_user_dataclass_has_no_id_field() -> None:
    fields = HeadlessAdapter().get_user_dataclass().__dataclass_fields__

    assert "id" not in fields
    assert "username" not in fields


def test_served_spec_user_schema_has_no_id() -> None:
    response = Client().get("/_allauth/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert "id" not in spec["components"]["schemas"]["User"]["properties"]
    assert "/_allauth/{client}/v1/auth/login" in spec["paths"]


def test_spec_page_is_served() -> None:
    assert Client().get("/_allauth/openapi.html").status_code == 200


def test_mfa_and_user_sessions_are_installed_and_social_login_is_not() -> None:
    # User sessions list a player's sessions (#100, ADR 0047).
    assert "allauth.mfa" in settings.INSTALLED_APPS
    assert "allauth.usersessions" in settings.INSTALLED_APPS
    assert not [
        app
        for app in settings.INSTALLED_APPS
        if app.startswith("allauth.socialaccount")
    ]


def test_config_has_no_socialaccount_section() -> None:
    data = Client().get(f"{BROWSER}/config").json()["data"]

    assert "socialaccount" not in data
    assert data["account"]["login_methods"] == ["email"]


def test_config_offers_totp_and_recovery_codes_only() -> None:
    data = Client().get(f"{BROWSER}/config").json()["data"]

    assert sorted(data["mfa"]["supported_types"]) == ["recovery_codes", "totp"]
    assert data["mfa"]["passkey_login_enabled"] is False


def test_served_spec_lists_the_mfa_endpoints() -> None:
    paths = Client().get("/_allauth/openapi.json").json()["paths"]

    assert "/_allauth/{client}/v1/auth/2fa/authenticate" in paths
    assert "/_allauth/{client}/v1/auth/2fa/reauthenticate" in paths
    assert "/_allauth/{client}/v1/account/authenticators/totp" in paths
    assert "/_allauth/{client}/v1/account/authenticators/recovery-codes" in paths
    assert "/_allauth/{client}/v1/account/authenticators/webauthn" not in paths


def test_provider_redirect_is_not_found() -> None:
    client = browser_client()

    response = post(
        client,
        f"{BROWSER}/auth/provider/redirect",
        {"provider": "made-up", "callback_url": "/", "process": "login"},
        **csrf_header(client),
    )

    assert response.status_code == 404


def test_no_server_rendered_account_pages() -> None:
    assert Client().get("/accounts/login/").status_code == 404

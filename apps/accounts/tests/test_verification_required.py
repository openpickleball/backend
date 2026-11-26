"""Email verification cannot be bypassed (#100, ADR 0014).

An Account whose login address is not verified cannot log in, and the
session or token its signup or login attempt holds reaches no
account-security endpoint (password change, session list and end, MFA
management, reauthentication) and no OPEN API operation, on either
headless client. Made-up data only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    NEW_PASSWORD,
    PASSWORD,
    HeadlessSession,
    app_headers,
)

pytestmark = pytest.mark.django_db

EMAIL = "unverified.player@example.org"


def _pending_flows(response: Any) -> set[str]:
    return {
        flow["id"]
        for flow in response.json().get("data", {}).get("flows", [])
        if flow.get("is_pending")
    }


@pytest.fixture(params=["signup", "login"])
def session(request: pytest.FixtureRequest, base: str) -> HeadlessSession:
    """A session that signed up, or tried to log in, unverified."""
    session = HeadlessSession(base)
    if request.param == "signup":
        response = session.signup(email=EMAIL)
    else:
        account = UserFactory.create(email=EMAIL, password=PASSWORD)
        EmailAddress.objects.create(
            user=account, email=EMAIL, primary=True, verified=False
        )
        response = session.login(EMAIL)
    assert response.status_code == 401
    assert _pending_flows(response) == {"verify_email"}
    return session


@pytest.fixture(params=[APP, BROWSER])
def base(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_the_session_is_not_signed_in(session: HeadlessSession) -> None:
    assert session.get("auth/session").status_code == 401


@pytest.mark.parametrize(
    ("method", "endpoint", "body"),
    [
        ("get", "auth/sessions", None),
        ("delete_json", "auth/sessions", {"sessions": ["anything"]}),
        (
            "post",
            "account/password/change",
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        ),
        ("get", "account/authenticators", None),
        ("get", "account/authenticators/totp", None),
        ("get", "account/authenticators/recovery-codes", None),
        ("post", "auth/reauthenticate", {"password": PASSWORD}),
        ("post", "auth/2fa/reauthenticate", {"code": "123456"}),
        ("get", "account/email", None),
    ],
)
def test_no_account_security_endpoint_is_reached(
    session: HeadlessSession,
    method: str,
    endpoint: str,
    body: dict[str, Any] | None,
) -> None:
    call = getattr(session, method)
    response = call(endpoint, body) if body is not None else call(endpoint)

    assert response.status_code in (401, 410), (endpoint, response.status_code)


def test_no_api_operation_is_reached(session: HeadlessSession) -> None:
    headers = app_headers(session.token) if session.base == APP else {}

    response = session.client.get("/api/v1/accounts/me", headers=headers)

    assert response.status_code == 401

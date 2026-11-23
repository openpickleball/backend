"""Sign-up, email verification, login, logout, and password reset through
allauth's headless API, for the app and browser clients (issue #30).

Made-up addresses on example.org only. Every email a test sends is read
from django.core.mail.outbox.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    FRONTEND_URL,
    NEW_PASSWORD,
    PASSWORD,
    RESET_PREFIX,
    SIGNUP_DETAILS,
    VERIFY_PREFIX,
    app_headers,
    browser_client,
    csrf_header,
    key_from_link,
    link_in_last_mail,
    login,
    post,
    signup,
    verify_from_last_mail,
)

pytestmark = pytest.mark.django_db

EMAIL = "flow.player@example.org"


def _pending_flows(body: dict[str, Any]) -> list[str]:
    return [flow["id"] for flow in body["data"]["flows"] if flow.get("is_pending")]


# App client.


def test_app_signup_sends_one_verification_link_to_the_frontend() -> None:
    response = signup(Client(), APP, EMAIL)

    assert response.status_code == 401
    assert _pending_flows(response.json()) == ["verify_email"]
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [EMAIL]
    assert link_in_last_mail(VERIFY_PREFIX).startswith(VERIFY_PREFIX)


def test_app_login_before_verifying_is_refused() -> None:
    client = Client()
    signup(client, APP, EMAIL)
    mail.outbox.clear()

    response = login(Client(), APP, EMAIL)

    # The signup mail just went out, and allauth sends one verification
    # mail per address every 3 minutes, so the login sends none.
    assert mail.outbox == []
    assert response.status_code == 401
    body = response.json()
    assert body["meta"]["is_authenticated"] is False
    assert _pending_flows(body) == ["verify_email"]
    assert not EmailAddress.objects.get(email=EMAIL).verified
    # The app client gets a token that carries the pending flow only. It
    # does not authenticate.
    pending = body["meta"]["session_token"]
    session = Client().get(f"{APP}/auth/session", headers=app_headers(pending))
    assert session.status_code == 401
    assert session.json()["meta"]["is_authenticated"] is False


def test_app_verify_then_login_returns_a_session_token() -> None:
    signup(Client(), APP, EMAIL)

    verified = verify_from_last_mail(Client(), APP)
    response = login(Client(), APP, EMAIL)

    # Verifying by link does not log in: the client logs in next.
    assert verified.status_code == 401
    assert EmailAddress.objects.get(email=EMAIL).verified
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["is_authenticated"] is True
    assert body["meta"]["session_token"]
    assert body["data"]["user"]["email"] == EMAIL


def test_app_logout_ends_the_session() -> None:
    signup(Client(), APP, EMAIL)
    verify_from_last_mail(Client(), APP)
    client = Client()
    token = login(client, APP, EMAIL).json()["meta"]["session_token"]

    logout = client.delete(f"{APP}/auth/session", headers=app_headers(token))
    after = client.get(f"{APP}/auth/session", headers=app_headers(token))

    assert logout.status_code == 401
    assert after.status_code == 410


def test_app_login_with_a_wrong_password_fails() -> None:
    signup(Client(), APP, EMAIL)
    verify_from_last_mail(Client(), APP)

    response = login(Client(), APP, EMAIL, password="not-the-password")

    assert response.status_code == 400
    assert [error["param"] for error in response.json()["errors"]] == ["password"]


# Browser client.


def test_browser_login_without_the_csrf_token_is_refused() -> None:
    client = browser_client()

    response = login(client, BROWSER, EMAIL)

    assert response.status_code == 403


def test_browser_flow_signup_verify_login_logout() -> None:
    client = browser_client()

    signed_up = post(
        client,
        f"{BROWSER}/auth/signup",
        {"email": EMAIL, "password": PASSWORD, **SIGNUP_DETAILS},
        **csrf_header(client),
    )
    early = login(client, BROWSER, EMAIL, **csrf_header(client))
    verify_from_last_mail(client, BROWSER, **csrf_header(client))
    client.cookies.pop("sessionid", None)
    logged_in = login(client, BROWSER, EMAIL, **csrf_header(client))
    session_cookie = client.cookies["sessionid"].value
    session = client.get(f"{BROWSER}/auth/session")
    logged_out = client.delete(f"{BROWSER}/auth/session", headers=csrf_header(client))
    after = client.get(f"{BROWSER}/auth/session")

    assert signed_up.status_code == 401
    assert early.status_code == 401
    assert early.json()["meta"]["is_authenticated"] is False
    assert _pending_flows(early.json()) == ["verify_email"]
    assert logged_in.status_code == 200
    assert "session_token" not in logged_in.json()["meta"]
    assert session_cookie
    assert session.status_code == 200
    assert session.json()["meta"]["is_authenticated"] is True
    assert logged_out.status_code == 401
    assert after.status_code == 401
    assert after.json()["meta"]["is_authenticated"] is False


# Password reset by link.


def _verified_account() -> None:
    signup(Client(), APP, EMAIL)
    verify_from_last_mail(Client(), APP)
    mail.outbox.clear()


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_password_reset_by_link(base: str) -> None:
    _verified_account()
    client = browser_client() if base == BROWSER else Client()
    headers = csrf_header(client) if base == BROWSER else {}

    requested = post(
        client, f"{base}/auth/password/request", {"email": EMAIL}, **headers
    )
    assert requested.status_code == 200
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [EMAIL]
    key = key_from_link(link_in_last_mail(RESET_PREFIX), RESET_PREFIX)

    reset = post(
        client,
        f"{base}/auth/password/reset",
        {"key": key, "password": NEW_PASSWORD},
        **headers,
    )
    # A reset does not log in (ACCOUNT_LOGIN_ON_PASSWORD_RESET is off).
    assert reset.status_code == 401
    assert reset.json()["meta"]["is_authenticated"] is False

    old = login(Client(), APP, EMAIL, password=PASSWORD)
    new = login(Client(), APP, EMAIL, password=NEW_PASSWORD)
    assert old.status_code == 400
    assert new.status_code == 200


def test_password_reset_for_an_unknown_address_looks_the_same_and_sends_nothing() -> (
    None
):
    _verified_account()

    known = post(Client(), f"{APP}/auth/password/request", {"email": EMAIL})
    mail.outbox.clear()
    unknown = post(
        Client(), f"{APP}/auth/password/request", {"email": "nobody@example.org"}
    )

    assert unknown.status_code == known.status_code == 200
    assert unknown.json() == known.json()
    assert mail.outbox == []


def test_signup_with_a_taken_address_looks_like_a_new_signup() -> None:
    _verified_account()
    # The verification mail used up the address's send limit.
    cache.clear()

    response = signup(Client(), APP, EMAIL.upper())

    assert response.status_code == 401
    assert _pending_flows(response.json()) == ["verify_email"]
    assert EmailAddress.objects.count() == 1
    # The owner is told an account exists, and gets no verification link.
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [EMAIL]
    assert mail.outbox[0].subject == "You already have an OPEN account"
    assert VERIFY_PREFIX not in mail.outbox[0].body
    assert f"{FRONTEND_URL}/account/password/reset" in mail.outbox[0].body

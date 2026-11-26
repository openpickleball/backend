"""Helpers for tests of allauth's headless API (issue #30).

Made-up data only, never real player data. Requests go through Django's
test client and the whole middleware stack.
"""

import re
from importlib import import_module
from typing import Any
from urllib.parse import unquote

import pytest
from django.conf import settings
from django.contrib.sessions.backends.base import SessionBase
from django.core import mail
from django.http import HttpResponseBase
from django.test import Client, TestCase
from django.test.client import ClientHandler

APP = "/_allauth/app/v1"
BROWSER = "/_allauth/browser/v1"
# The web app origin the email links use, whatever the environment sets.
FRONTEND_URL: str = settings.FRONTEND_URL
PASSWORD = "made-up-Passw0rd-for-tests"
NEW_PASSWORD = "another-made-up-Passw0rd-2"
# The age screen's fields for a made-up adult (issue #114).
ADULT_BIRTH = {"birth_year": 1990, "birth_month": 6}
# Every signup detail besides the email and password: the age, the
# gender division (issue #215), a made-up name and a place (#221), and
# the acceptance of the legal agreements (#222).
SIGNUP_GENDER_DIVISION = "womens"
SIGNUP_NAMES = {"first_name": "Robin", "last_name": "Quillfeather"}
SIGNUP_LOCATION = {"city": "Lakeview", "region": "Oregon"}
# The legal name and starting display name SIGNUP_NAMES give.
SIGNUP_FULL_NAME = "Robin Quillfeather"
SIGNUP_DETAILS = {
    **ADULT_BIRTH,
    "gender_division": SIGNUP_GENDER_DIVISION,
    **SIGNUP_NAMES,
    **SIGNUP_LOCATION,
    "accepted_terms": True,
    "accepted_privacy": True,
}

VERIFY_PREFIX = f"{FRONTEND_URL}/account/verify-email/"
RESET_PREFIX = f"{FRONTEND_URL}/account/password/reset/key/"


def post(client: Client, path: str, data: dict[str, Any], **headers: str) -> Any:
    """POST a JSON body and return the response."""
    return client.post(path, data, content_type="application/json", headers=headers)


def app_headers(token: str | None) -> dict[str, str]:
    """The app client's session token header, when there is a token."""
    return {"X-Session-Token": token} if token else {}


def signup(
    client: Client,
    base: str,
    email: str,
    password: str = PASSWORD,
    **headers: str,
) -> Any:
    """Sign up with a made-up adult age, a gender division, a name, and a
    place (#114, #215, #221)."""
    data = {"email": email, "password": password, **SIGNUP_DETAILS}
    return post(client, f"{base}/auth/signup", data, **headers)


def login(
    client: Client,
    base: str,
    email: str,
    password: str = PASSWORD,
    **headers: str,
) -> Any:
    return post(
        client,
        f"{base}/auth/login",
        {"email": email, "password": password},
        **headers,
    )


def link_in_last_mail(prefix: str) -> str:
    """The one link in the last email that starts with prefix."""
    body = str(mail.outbox[-1].body)
    links = re.findall(re.escape(prefix) + r"\S+", body)
    assert len(links) == 1, body
    return str(links[0])


def key_from_link(link: str, prefix: str) -> str:
    """The key in a link's path, decoded as the web app's router would."""
    return unquote(link.removeprefix(prefix))


def verify_from_last_mail(client: Client, base: str, **headers: str) -> Any:
    """Verify the address with the key from the last email's link."""
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    return post(client, f"{base}/auth/email/verify", {"key": key}, **headers)


def browser_client() -> Client:
    """A browser client that enforces CSRF, holding a CSRF cookie."""
    client = Client(enforce_csrf_checks=True)
    response = client.get(f"{BROWSER}/config")
    assert response.status_code == 200
    return client


def csrf_header(client: Client) -> dict[str, str]:
    return {"X-CSRFToken": client.cookies[settings.CSRF_COOKIE_NAME].value}


def run_on_commit_after_each_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the on_commit callbacks a test client request registers.

    In production a request's transactions commit before the response,
    and the queued email task then sends the message (ADR 0016). A test
    runs inside one transaction that never commits, so this runs each
    request's callbacks when it returns, as a commit would. Mail then
    reaches ``mail.outbox`` when the response comes back, as before.
    """
    original = ClientHandler.__call__

    def call(self: ClientHandler, environ: dict[str, Any]) -> HttpResponseBase:
        with TestCase.captureOnCommitCallbacks(execute=True):
            response: HttpResponseBase = original(self, environ)
        return response

    monkeypatch.setattr(ClientHandler, "__call__", call)


# A made-up address for HeadlessSession.signup.
SESSION_EMAIL = "age.player@example.org"


class HeadlessSession:
    """One headless client's session: a cookie jar or a session token."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.client = browser_client() if base == BROWSER else Client()
        self.token: str | None = None

    def _headers(self) -> dict[str, str]:
        if self.base == BROWSER:
            return csrf_header(self.client)
        return app_headers(self.token)

    def _keep_token(self, response: Any) -> Any:
        if self.base == APP and response.content:
            token = response.json().get("meta", {}).get("session_token")
            if token:
                self.token = token
        return response

    def post(self, endpoint: str, data: dict[str, Any]) -> Any:
        response = post(self.client, f"{self.base}/{endpoint}", data, **self._headers())
        return self._keep_token(response)

    def get(self, endpoint: str) -> Any:
        response = self.client.get(f"{self.base}/{endpoint}", headers=self._headers())
        return self._keep_token(response)

    def delete(self, endpoint: str) -> Any:
        response = self.client.delete(
            f"{self.base}/{endpoint}", headers=self._headers()
        )
        return self._keep_token(response)

    def login(self, email: str, password: str = PASSWORD) -> Any:
        return self.post("auth/login", {"email": email, "password": password})

    def reauthenticate(self, password: str = PASSWORD) -> Any:
        return self.post("auth/reauthenticate", {"password": password})

    def logout(self) -> Any:
        response = self.delete("auth/session")
        if self.base == APP:
            self.token = None
        return response

    def mfa_login(self, email: str, code: str, password: str = PASSWORD) -> Any:
        """Log in with the password, then pass the MFA stage with ``code``
        (a TOTP code or a recovery code)."""
        response = self.login(email, password)
        flows = response.json()["data"]["flows"]
        assert any(
            flow["id"] == "mfa_authenticate" and flow.get("is_pending")
            for flow in flows
        ), flows
        return self.post("auth/2fa/authenticate", {"code": code})

    def signup(self, email: object = SESSION_EMAIL, **fields: object) -> Any:
        data: dict[str, object] = {"email": email, "password": PASSWORD}
        data.update(SIGNUP_DETAILS)
        data.update(fields)
        return self.post("auth/signup", {k: v for k, v in data.items() if v is not ...})

    def age_check(self, **fields: object) -> Any:
        """The signup age check (#223), with a made-up adult age unless
        ``fields`` replace it. A field set to ``...`` is left out."""
        data: dict[str, object] = {**ADULT_BIRTH, **fields}
        return self.post(
            "auth/signup/age-check", {k: v for k, v in data.items() if v is not ...}
        )

    def password_check(self, **fields: object) -> Any:
        """The signup password check (#289), with ``SESSION_EMAIL`` and
        ``PASSWORD`` unless ``fields`` replace them. A field set to ``...``
        is left out."""
        data: dict[str, object] = {
            "email": SESSION_EMAIL,
            "password": PASSWORD,
            **fields,
        }
        return self.post(
            "auth/signup/password-check",
            {k: v for k, v in data.items() if v is not ...},
        )

    def store(self) -> SessionBase:
        if self.base == BROWSER:
            return self.client.session
        store: SessionBase = import_module(settings.SESSION_ENGINE).SessionStore(
            session_key=self.token
        )
        return store

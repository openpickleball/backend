"""``auth_session``: the session behind a request's authentication (#124).

The app client's session is the one its ``X-Session-Token`` names, not
``request.session`` (which, with no cookie, is a new empty session). The
browser client's is ``request.session``. Views that read or change a
value the session holds (the signup age, ADR 0017) go through
``auth_session``. Made-up data only.
"""

from importlib import import_module

import pytest
from django.conf import settings
from django.contrib.sessions.backends.base import SessionBase
from django.test import Client, RequestFactory

from apps.accounts.tests.factories import UserFactory
from config.auth import SessionTokenAuth, auth_session

pytestmark = pytest.mark.django_db


def _store(key: str | None = None) -> SessionBase:
    store: SessionBase = import_module(settings.SESSION_ENGINE).SessionStore(
        session_key=key
    )
    return store


def _signed_in_session_key() -> str:
    client = Client()
    client.force_login(UserFactory.create())
    key = client.session.session_key
    assert key is not None
    return key


def test_the_token_session_is_the_auth_session() -> None:
    key = _signed_in_session_key()
    request = RequestFactory().get("/api/v1/me/player", HTTP_X_SESSION_TOKEN=key)
    request.session = _store()

    assert SessionTokenAuth()(request) is not None

    assert auth_session(request).session_key == key


def test_a_change_saved_to_the_token_session_keeps_its_key() -> None:
    key = _signed_in_session_key()
    request = RequestFactory().get("/api/v1/me/player", HTTP_X_SESSION_TOKEN=key)
    request.session = _store()
    SessionTokenAuth()(request)

    session = auth_session(request)
    session["made_up_value"] = 1
    session.save()

    assert session.session_key == key
    assert _store(key)["made_up_value"] == 1


def test_without_a_token_the_auth_session_is_the_request_session() -> None:
    request = RequestFactory().get("/api/v1/me/player")
    request.session = _store(_signed_in_session_key())

    assert auth_session(request) is request.session


def test_a_refused_token_leaves_the_request_session() -> None:
    request = RequestFactory().get(
        "/api/v1/me/player", HTTP_X_SESSION_TOKEN="not-a-session-key"
    )
    request.session = _store()

    assert SessionTokenAuth()(request) is None

    assert auth_session(request) is request.session

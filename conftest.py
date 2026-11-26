"""Shared pytest fixtures.

The plain `client` fixture (Django's test client, not logged in) comes from
pytest-django. `api_client` is django-ninja's test client for the real API in
`config/api.py`: it calls operations directly, so paths are relative to
/api/v1/ (`api_client.get("/openapi.json")`) and no middleware runs.
`Client.force_login` stands in for a whole login: it starts the session's
lifetime clocks (#100), and for an Account with MFA it also records that the session passed the second factor, as the real
MFA login does (ADR 0043), so tests of signed-in members and staff keep
testing what they did. Tests of the MFA rules use real logins.
`commit_after_each_request` sends queued email when each test client
request returns, as a commit would; tests that read allauth's emails use
it (the accounts tests always do). `breach_service` stands in for the
breached password service in every test, so none reaches the network.
"""

from typing import TYPE_CHECKING, Any

import pytest
from django.test import Client
from ninja.testing import TestClient

from apps.accounts import breached
from apps.accounts.mfa import has_mfa
from apps.accounts.sessions import start_lifetime
from apps.accounts.tests.breach import FakeBreachService
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import pass_mfa, run_on_commit_after_each_request
from config.api import api

if TYPE_CHECKING:
    # Resolved to AUTH_USER_MODEL by the django-stubs plugin.
    from django.contrib.auth.models import _User


@pytest.fixture
def user(db: None) -> "_User":
    """A made-up Account from UserFactory."""
    return UserFactory.create()


@pytest.fixture
def user_client(client: Client, user: "_User") -> Client:
    """Django's test client, logged in as `user`."""
    client.force_login(user)
    return client


@pytest.fixture
def api_client() -> TestClient:
    """django-ninja's test client for the real OPEN API, not logged in."""
    return TestClient(api)


@pytest.fixture
def commit_after_each_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test client request's on_commit callbacks when it returns.

    Queued email (ADR 0016) is sent after commit, and a test transaction
    never commits. With this fixture, an allauth email reaches
    ``mail.outbox`` by the time the response comes back.
    """
    run_on_commit_after_each_request(monkeypatch)


@pytest.fixture(autouse=True)
def breach_service(monkeypatch: pytest.MonkeyPatch) -> FakeBreachService:
    """The breached password service, replaced for every test (#100).

    No test reaches the network. It knows no breached password unless a
    test adds one (``breach_service.add(password)``).
    """
    service = FakeBreachService(original=breached.fetch_range)
    monkeypatch.setattr(breached, "fetch_range", service)
    return service


@pytest.fixture(autouse=True)
def _force_login_passes_mfa(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Client.force_login`` as a whole login: the session's lifetime
    clocks start (#100), and for an Account with MFA it passed MFA."""
    original = Client.force_login

    def force_login(self: Client, user: Any, backend: str | None = None) -> None:
        original(self, user, backend)
        session = self.session
        start_lifetime(session)
        session.save()
        if has_mfa(user):
            pass_mfa(self.session, user)

    monkeypatch.setattr(Client, "force_login", force_login)

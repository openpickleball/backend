"""Shared pytest fixtures.

The plain `client` fixture (Django's test client, not logged in) comes from
pytest-django. `api_client` is django-ninja's test client for the real API in
`config/api.py`: it calls operations directly, so paths are relative to
/api/v1/ (`api_client.get("/openapi.json")`) and no middleware runs.
"""

from typing import TYPE_CHECKING

import pytest
from django.test import Client
from ninja.testing import TestClient

from apps.accounts.tests.factories import UserFactory
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

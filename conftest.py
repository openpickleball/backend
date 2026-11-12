"""Shared pytest fixtures.

The plain `client` fixture (Django's test client, not logged in) comes from
pytest-django. #5 adds the django-ninja API client fixture here.
"""

from typing import TYPE_CHECKING

import pytest
from django.test import Client

from apps.accounts.tests.factories import UserFactory

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

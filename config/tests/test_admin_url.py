"""The admin is served at ``settings.ADMIN_URL`` (issue #101).

The URLconf reads the setting at import, so each test reloads it under
the overridden value and reloads it again afterwards.
"""

import importlib
from collections.abc import Iterator

import pytest
from django.test import Client, override_settings
from django.urls import clear_url_caches, reverse

import config.urls

pytestmark = pytest.mark.django_db

PRIVATE = "staff-test-x7/"


def _reload_urls() -> None:
    importlib.reload(config.urls)
    clear_url_caches()


@pytest.fixture
def private_admin() -> Iterator[None]:
    with override_settings(ADMIN_URL=PRIVATE):
        _reload_urls()
        yield
    _reload_urls()


def test_the_admin_is_at_the_private_path(private_admin: None) -> None:
    assert reverse("admin:index") == f"/{PRIVATE}"
    assert reverse("admin:login") == f"/{PRIVATE}login/"


def test_the_well_known_path_is_not_found(private_admin: None, client: Client) -> None:
    for path in ("/admin/", "/admin/login/", "/admin/accounts/user/"):
        assert client.get(path).status_code == 404, path


def test_the_private_path_serves_the_login_page(
    private_admin: None, client: Client
) -> None:
    assert client.get(f"/{PRIVATE}login/").status_code == 200


def test_the_local_default_is_admin(client: Client) -> None:
    assert reverse("admin:index") == "/admin/"
    assert client.get("/admin/login/").status_code == 200

"""pytest runs the suite with the same test overrides as `manage.py test`.

pytest loads `config.settings_test`, which turns off the https redirect
and swaps the cache for LocMemCache in the settings module itself, so
every process that loads it (parallel workers included) gets them.
"""

import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import connection
from django.test import Client

from config.test_runner import _TEST_OVERRIDES

BASE_DIR = Path(__file__).resolve().parent.parent.parent

PROBE_TESTS = """
import os

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.test import Client


def test_settings_module_is_the_test_one():
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings_test"
    assert settings.DEBUG is False


def test_serves_plain_http():
    assert settings.SECURE_SSL_REDIRECT is False
    response = Client().get("/no-such-page/")
    assert response.status_code == 404


def test_uses_in_memory_cache():
    cache = caches["default"]
    assert isinstance(cache, LocMemCache)
    cache.set("probe-key", "ok")
    assert cache.get("probe-key") == "ok"
"""


def test_settings_module_is_the_test_one() -> None:
    # A DJANGO_SETTINGS_MODULE exported in the shell overrides the pytest
    # config; fail loudly rather than test production settings.
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings_test"


@pytest.mark.django_db
def test_database_is_postgres() -> None:
    assert connection.vendor == "postgresql"


@pytest.mark.parametrize("name", sorted(_TEST_OVERRIDES))
def test_overrides_match_the_test_runner(name: str) -> None:
    import config.settings_test

    assert getattr(config.settings_test, name) == _TEST_OVERRIDES[name]


def test_settings_module_overrides_only_the_test_runner_overrides() -> None:
    import config.settings
    import config.settings_test

    changed = {
        name
        for name in dir(config.settings_test)
        if name.isupper()
        and getattr(config.settings_test, name)
        != getattr(config.settings, name, object())
    }
    assert changed == set(_TEST_OVERRIDES)


def test_active_settings_carry_the_overrides() -> None:
    assert settings.SECURE_SSL_REDIRECT is False
    assert _TEST_OVERRIDES["CACHES"] == settings.CACHES


@pytest.mark.django_db
def test_user_client_is_logged_in_as_user(
    user_client: Client, user: AbstractUser
) -> None:
    assert user_client.session["_auth_user_id"] == str(user.pk)


def test_debug_off_run_keeps_test_overrides() -> None:
    """A DEBUG-off pytest run serves plain http and needs no cache server.

    This runs a small database-free probe file through pytest with the
    repo's pytest config in a fresh process. REDIS_URL points at a closed
    port, so a pass proves no cache server is used. Nothing is written
    outside a temporary directory.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "test_probe.py").write_text(PROBE_TESTS)
        env = {
            "PATH": os.environ.get("PATH", ""),
            # Lets the probe import config; DJANGO_SETTINGS_MODULE is left
            # unset so the repo's pytest config chooses the settings module.
            "PYTHONPATH": str(BASE_DIR),
            # Every settings variable is set, so no .env value is used.
            "DJANGO_DEBUG": "False",
            # Generated at runtime for this test only.
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
            "DJANGO_ALLOWED_HOSTS": "",
            "DATABASE_URL": os.environ["DATABASE_URL"],
            # Nothing listens here. The cache must not be used.
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "CSRF_TRUSTED_ORIGINS": "",
            "CORS_ALLOWED_ORIGINS": "",
        }
        if "HOME" in os.environ:
            env["HOME"] = os.environ["HOME"]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                str(BASE_DIR / "pyproject.toml"),
                "--rootdir",
                str(tmp_path),
                "-p",
                "no:cacheprovider",
                "--no-cov",
                # pytest-django passes -v on as Django's verbosity, and Django
                # writes "Creating test database" only at verbosity 1 or
                # more. -s stops pytest capturing that line during setup.
                "-v",
                "-s",
                str(tmp_path / "test_probe.py"),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "3 passed" in output
    # The probe never touches the database, so it cannot collide with the
    # outer run's test database. -v -s make a database creation visible.
    assert "Creating test database" not in output

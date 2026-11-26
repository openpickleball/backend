"""Tests serve the admin at admin/ whatever ADMIN_URL says (issue #101).

A developer's .env may set ADMIN_URL to a private path, as production
does. The test overrides pin it to admin/, in both the pytest settings
module and the manage.py test runner, so tests that use /admin/ keep
working. Made-up values only.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from config.test_runner import _TEST_OVERRIDES

BASE_DIR = Path(__file__).resolve().parent.parent.parent

PROBE = """
from django.conf import settings
from django.urls import reverse


def test_the_admin_is_at_admin():
    assert settings.ADMIN_URL == "admin/"
    assert reverse("admin:index") == "/admin/"
"""


def test_both_override_sets_pin_admin_url() -> None:
    import config.settings_test

    assert _TEST_OVERRIDES["ADMIN_URL"] == "admin/"
    assert config.settings_test.ADMIN_URL == "admin/"


def test_a_private_admin_url_in_the_environment_does_not_reach_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "test_probe.py").write_text(PROBE)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(BASE_DIR),
            "DJANGO_DEBUG": "True",
            "DJANGO_SECRET_KEY": "",
            "DJANGO_ALLOWED_HOSTS": "",
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "DIRECT_DATABASE_URL": "",
            # Nothing listens here. The cache must not be used.
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "CSRF_TRUSTED_ORIGINS": "",
            "CORS_ALLOWED_ORIGINS": "",
            "FRONTEND_URL": "http://localhost:8081",
            "ADMIN_URL": "staff-x7k2/",
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
    assert "1 passed" in output

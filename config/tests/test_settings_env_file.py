"""Tests for reading an optional .env file (issue #4, criterion 2).

Each case copies manage.py and config/ into a temporary project directory
(apps/ is symlinked) and writes that directory's own .env, so the
developer's real .env is never read or touched. Unlike test_settings,
variables can be truly unset here.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from config.tests.test_settings import SETTINGS_VARS, TEST_SECRET_KEY

BASE_DIR = Path(__file__).resolve().parent.parent.parent

PRINT_SETTINGS = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps({
    'DEBUG': settings.DEBUG,
    'SECRET_KEY': settings.SECRET_KEY,
    'ALLOWED_HOSTS': settings.ALLOWED_HOSTS,
    'REDIS_URL': settings.REDIS_URL,
    'DB_HOST': settings.DATABASES['default']['HOST'],
}))
"""

DOT_ENV = """\
# Test-only values.
DJANGO_SECRET_KEY=
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=dotenv.example
DATABASE_URL=postgres://open@dotenv-db.example:5432/open
REDIS_URL=redis://dotenv-cache.example:6379/0
CSRF_TRUSTED_ORIGINS=
"""


class EnvFileTests(SimpleTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        shutil.copy2(BASE_DIR / "manage.py", self.project / "manage.py")
        shutil.copytree(
            BASE_DIR / "config",
            self.project / "config",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (self.project / "apps").symlink_to(BASE_DIR / "apps")

    def _write_env(self, content: str) -> None:
        (self.project / ".env").write_text(content)

    def _run(
        self, args: list[str], values: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """Run with only the given settings variables set; others are unset."""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "DJANGO_SETTINGS_MODULE": "config.settings",
        }
        if "HOME" in os.environ:
            env["HOME"] = os.environ["HOME"]
        for name, value in values.items():
            assert name in SETTINGS_VARS, name
            env[name] = value
        return subprocess.run(
            [sys.executable, *args],
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _settings(self, values: dict[str, str]) -> dict[str, object]:
        result = self._run(["-c", PRINT_SETTINGS], values)
        if result.returncode != 0:
            raise AssertionError(f"settings failed to load:\n{result.stderr}")
        settings: dict[str, object] = json.loads(result.stdout)
        return settings

    def test_env_file_is_read_when_present(self) -> None:
        self._write_env(DOT_ENV)
        values = self._settings({})
        self.assertIs(values["DEBUG"], True)
        self.assertEqual(values["ALLOWED_HOSTS"], ["dotenv.example"])
        self.assertEqual(values["DB_HOST"], "dotenv-db.example")
        self.assertEqual(values["REDIS_URL"], "redis://dotenv-cache.example:6379/0")

    def test_process_env_wins_over_env_file(self) -> None:
        self._write_env(DOT_ENV)
        values = self._settings(
            {
                "DJANGO_DEBUG": "False",
                "DJANGO_SECRET_KEY": TEST_SECRET_KEY,
                "DJANGO_ALLOWED_HOSTS": "process.example",
            }
        )
        self.assertIs(values["DEBUG"], False)
        self.assertEqual(values["SECRET_KEY"], TEST_SECRET_KEY)
        self.assertEqual(values["ALLOWED_HOSTS"], ["process.example"])
        self.assertEqual(values["DB_HOST"], "dotenv-db.example")

    def test_starts_without_env_file_from_process_env(self) -> None:
        values = self._settings(
            {
                "DJANGO_DEBUG": "True",
                "DATABASE_URL": "postgres://open@process-db.example:5432/open",
                "REDIS_URL": "redis://process-cache.example:6379/0",
            }
        )
        self.assertIs(values["DEBUG"], True)
        self.assertEqual(values["ALLOWED_HOSTS"], [])
        self.assertEqual(values["DB_HOST"], "process-db.example")

    def test_unset_variables_without_env_file_fail(self) -> None:
        result = self._run(["manage.py", "check"], {})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_unset_database_url_fails(self) -> None:
        values = {
            "DJANGO_DEBUG": "True",
            "REDIS_URL": "redis://process-cache.example:6379/0",
        }
        result = self._run(["manage.py", "check"], values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL", result.stderr)

    def test_dollar_values_in_env_file_stay_literal(self) -> None:
        key = "$" + TEST_SECRET_KEY
        # A valid Redis URL, so settings load, with a $ in the password.
        redis_url = "redis://u:$HOME@dotenv-cache.example:6379/0"
        self._write_env(
            DOT_ENV.replace("DJANGO_SECRET_KEY=\n", f"DJANGO_SECRET_KEY={key}\n")
            .replace("DJANGO_DEBUG=True", "DJANGO_DEBUG=False")
            .replace(
                "REDIS_URL=redis://dotenv-cache.example:6379/0",
                f"REDIS_URL={redis_url}",
            )
        )
        values = self._settings({})
        self.assertIs(values["DEBUG"], False)
        self.assertEqual(values["SECRET_KEY"], key)
        self.assertEqual(values["REDIS_URL"], redis_url)

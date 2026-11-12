"""The test runner keeps http in parallel workers started with spawn.

With DEBUG off, settings turn on SECURE_SSL_REDIRECT. The test runner turns
it off for the test run, and that must also hold in parallel workers that
re-import settings (spawn start method, the default on macOS and Windows).
This runs a small probe suite with --parallel 2 under spawn in a fresh
process. Nothing is written outside a temporary directory.
"""

import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent.parent

PROBE_TESTS = """
import multiprocessing

from django.conf import settings
from django.test import SimpleTestCase


class _Probe:
    def test_worker_serves_plain_http(self):
        # Proves the test runs in a parallel worker, not the parent.
        self.assertIsNotNone(multiprocessing.parent_process())
        self.assertIs(settings.DEBUG, False)
        self.assertIs(settings.SECURE_SSL_REDIRECT, False)
        response = self.client.get('/no-such-page/')
        self.assertEqual(response.status_code, 404)


class FirstProbeTests(_Probe, SimpleTestCase):
    pass


class SecondProbeTests(_Probe, SimpleTestCase):
    pass
"""

RUN_SPAWN = f"""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.set_start_method('spawn', force=True)
    sys.path.insert(0, {str(BASE_DIR)!r})
    from django.core.management import execute_from_command_line

    execute_from_command_line(
        ['manage.py', 'test', 'spawn_probe_tests', '--parallel', '2']
    )
"""


class ParallelSpawnTests(SimpleTestCase):
    def test_spawned_workers_keep_ssl_redirect_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "spawn_probe_tests.py").write_text(PROBE_TESTS)
            (tmp_path / "run_spawn.py").write_text(RUN_SPAWN)
            env = {
                "PATH": os.environ.get("PATH", ""),
                "DJANGO_SETTINGS_MODULE": "config.settings",
                # Every settings variable is set, so no .env value is used.
                "DJANGO_DEBUG": "False",
                # Generated at runtime for this test only.
                "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
                "DJANGO_ALLOWED_HOSTS": "",
                "DATABASE_URL": "postgres://open@localhost:5432/open",
                "REDIS_URL": "redis://localhost:6379/0",
                "CSRF_TRUSTED_ORIGINS": "",
            }
            if "HOME" in os.environ:
                env["HOME"] = os.environ["HOME"]
            result = subprocess.run(
                [sys.executable, "run_spawn.py"],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Ran 2 tests", output)
        self.assertIn("OK", output)

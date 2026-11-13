"""Run the real gunicorn against gunicorn.conf.py (issue #10).

gunicorn parses WEB_CONCURRENCY with int() when it is imported, before
it reads gunicorn.conf.py. So a non-integer stops startup with
gunicorn's own error, and gunicorn.conf.py refuses the integers below 1
that gunicorn accepts. `gunicorn --check-config` loads the config file
and the app without serving.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from config.tests.test_settings import PRODUCTION_LIKE, _env

BASE_DIR = Path(__file__).resolve().parent.parent.parent
GUNICORN = Path(sys.executable).parent / "gunicorn"


def _gunicorn(*args: str, **values: str) -> subprocess.CompletedProcess[str]:
    env = _env(PRODUCTION_LIKE)
    env.update(values)
    return subprocess.run(
        [str(GUNICORN), *args],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_defaults_pass_the_config_check() -> None:
    result = _gunicorn("--check-config")
    assert result.returncode == 0, result.stderr


def test_print_config_shows_the_file_settings() -> None:
    result = _gunicorn("--print-config", PORT="9000", WEB_CONCURRENCY="3")
    assert result.returncode == 0, result.stderr
    lines = {
        name.strip(): value.strip()
        for name, _, value in (
            line.partition("=") for line in result.stdout.splitlines()
        )
    }
    assert lines["wsgi_app"] == "config.wsgi:application"
    assert lines["bind"] == "['0.0.0.0:9000']"
    assert lines["workers"] == "3"


def test_zero_workers_fails_with_the_config_message() -> None:
    result = _gunicorn("--check-config", WEB_CONCURRENCY="0")
    assert result.returncode != 0
    error = result.stderr.strip().splitlines()[-1]
    assert error == (
        "ValueError: Set WEB_CONCURRENCY to a whole number of at least 1, "
        "or leave it unset."
    )


@pytest.mark.parametrize("value", ["abc", "", "1.5"])
def test_non_integer_workers_stops_startup(value: str) -> None:
    result = _gunicorn("--check-config", WEB_CONCURRENCY=value)
    assert result.returncode != 0
    assert "ValueError" in result.stderr

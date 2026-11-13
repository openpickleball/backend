"""gunicorn's control socket stays off (issue #10).

gunicorn 27 opens a Unix control socket by default, which accepts
reload, shutdown, and worker commands. Nothing uses it, so
gunicorn.conf.py turns it off.
"""

import runpy

import pytest

from config.tests.test_gunicorn_conf import CONF
from config.tests.test_gunicorn_startup import _gunicorn


def test_config_file_disables_the_control_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    conf = runpy.run_path(str(CONF))
    assert conf["control_socket_disable"] is True


def test_gunicorn_reads_the_control_socket_as_disabled() -> None:
    result = _gunicorn("--print-config")
    assert result.returncode == 0, result.stderr
    settings = {
        name.strip(): value.strip()
        for name, _, value in (
            line.partition("=") for line in result.stdout.splitlines()
        )
    }
    assert settings["control_socket_disable"] == "True"

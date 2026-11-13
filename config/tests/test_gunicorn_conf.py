"""Tests for gunicorn.conf.py (issue #10).

gunicorn reads gunicorn.conf.py from the working directory, so
`uv run gunicorn` at the repo root needs no arguments. The port and the
worker count come from PORT and WEB_CONCURRENCY. A bad value stops
startup with an error that names the variable and never echoes it.
"""

import runpy
from pathlib import Path
from typing import Any

import pytest

CONF = Path(__file__).resolve().parent.parent.parent / "gunicorn.conf.py"


def _load(monkeypatch: pytest.MonkeyPatch, **values: str | None) -> dict[str, Any]:
    for name in ("PORT", "WEB_CONCURRENCY"):
        value = values.get(name)
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return runpy.run_path(str(CONF))


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = _load(monkeypatch)
    assert conf["wsgi_app"] == "config.wsgi:application"
    assert conf["bind"] == "0.0.0.0:8000"
    assert conf["workers"] == 2


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_values_mean_unset(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    conf = _load(monkeypatch, PORT=blank, WEB_CONCURRENCY=blank)
    assert conf["bind"] == "0.0.0.0:8000"
    assert conf["workers"] == 2


def test_port_and_workers_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conf = _load(monkeypatch, PORT="9000", WEB_CONCURRENCY="4")
    assert conf["bind"] == "0.0.0.0:9000"
    assert conf["workers"] == 4


@pytest.mark.parametrize("port", ["1", "65535", " 8765 "])
def test_port_range_edges_are_accepted(
    monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    conf = _load(monkeypatch, PORT=port)
    assert conf["bind"] == f"0.0.0.0:{port.strip()}"


# Made-up bad values. Each is checked against the error message.
BAD_PORTS = ["0", "65536", "abc", "-1", "1.5", "80x", "+80", "٨٠", "99999999"]
BAD_WORKERS = ["0", "abc", "-1", "1.5", "2x", "+2", "٤"]


@pytest.mark.parametrize("value", BAD_PORTS)
def test_bad_port_fails_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValueError, match="PORT") as error:
        _load(monkeypatch, PORT=value)
    assert value not in str(error.value)


@pytest.mark.parametrize("value", BAD_WORKERS)
def test_bad_web_concurrency_fails_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValueError, match="WEB_CONCURRENCY") as error:
        _load(monkeypatch, WEB_CONCURRENCY=value)
    assert value not in str(error.value)

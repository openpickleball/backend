"""Tests for gunicorn.conf.py (issue #10).

gunicorn reads gunicorn.conf.py from the working directory, so
`uv run gunicorn` at the repo root needs no arguments. The port and the
worker count come from PORT and WEB_CONCURRENCY. A bad PORT, or a
WEB_CONCURRENCY that is a number below 1, stops startup with an error
that names the variable and never echoes it. gunicorn itself parses
WEB_CONCURRENCY with int() before it reads this file, so a blank or
non-integer value stops startup with gunicorn's own error
(test_gunicorn_startup.py runs the real gunicorn).
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
def test_blank_port_means_unset(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    conf = _load(monkeypatch, PORT=blank)
    assert conf["bind"] == "0.0.0.0:8000"


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_web_concurrency_is_refused(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # gunicorn refuses a blank value before this file loads, so the file
    # must not claim blank means the default.
    with pytest.raises(ValueError, match="WEB_CONCURRENCY"):
        _load(monkeypatch, WEB_CONCURRENCY=blank)


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
# Values gunicorn's own int() accepts, but this file refuses.
BAD_WORKERS = ["0", "-1", "-0", "+2", "٤"]


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

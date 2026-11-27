"""The WebAuthn relying party ID and allowed origins (issue #117).

``WEBAUTHN_RP_ID`` is the registrable domain, ``openpb.org`` unless the
environment sets another, never the request's host. A bad value stops
startup, naming the variable and never echoing the value.
``WEBAUTHN_ALLOWED_ORIGINS`` defaults to the PWA's origin
(``FRONTEND_URL``) only. The system check ``accounts.W001`` warns when an
allowed origin is not under the relying party ID. Settings cases run in
a fresh process, as in ``config/tests/test_settings.py``.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.accounts.checks import check_webauthn_origins
from config.tests.test_settings import (
    BASE_DIR,
    LOCAL_DEBUG,
    PRODUCTION_LIKE,
    _check,
    _run,
)

RP_ID = "WEBAUTHN_RP_ID"
ORIGINS = "WEBAUTHN_ALLOWED_ORIGINS"

# A made-up host that must never appear in any output.
MARKER = "rp-marker-z8q"

PRINT_WEBAUTHN = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps([settings.WEBAUTHN_RP_ID, settings.WEBAUTHN_ALLOWED_ORIGINS]))
"""


def _webauthn(values: dict[str, str]) -> list[Any]:
    result = _run(["-c", PRINT_WEBAUTHN], values)
    assert result.returncode == 0, result.stderr
    loaded: list[Any] = json.loads(result.stdout)
    return loaded


def test_rp_id_defaults_to_the_registrable_domain() -> None:
    values = {**PRODUCTION_LIKE, RP_ID: ""}

    assert _webauthn(values)[0] == "openpb.org"


def test_unset_variables_give_the_defaults(tmp_path: Path) -> None:
    """The variables truly unset, in a copy of the project with no .env,
    so neither a developer's .env nor .env.example's values leak in (as
    config/tests/test_settings_env_file.py does)."""
    shutil.copy2(BASE_DIR / "manage.py", tmp_path / "manage.py")
    shutil.copytree(
        BASE_DIR / "config",
        tmp_path / "config",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tmp_path / "apps").symlink_to(BASE_DIR / "apps")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_SETTINGS_MODULE": "config.settings",
        **PRODUCTION_LIKE,
    }
    env.pop(RP_ID, None)
    env.pop(ORIGINS, None)
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]

    result = subprocess.run(
        [sys.executable, "-c", PRINT_WEBAUTHN],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "openpb.org",
        [PRODUCTION_LIKE["FRONTEND_URL"]],
    ]
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("value", ["example.org", "localhost", "sub-1.example.org"])
def test_rp_id_takes_a_dns_name(value: str) -> None:
    assert _webauthn({**LOCAL_DEBUG, RP_ID: value})[0] == value


@pytest.mark.parametrize(
    "value",
    [
        f"https://{MARKER}.example.org",
        f"{MARKER}.example.org:443",
        f"{MARKER}.example.org/",
        f"{MARKER}.Example.org",
        f"{MARKER}.example.org.",
        MARKER,
        f"-{MARKER}.example.org",
        f"{MARKER}..example.org",
        f"{MARKER}_x.example.org",
        f"{MARKER}.example.123",
    ],
)
def test_a_bad_rp_id_stops_startup_without_echoing_it(value: str) -> None:
    result = _check({**PRODUCTION_LIKE, RP_ID: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert RP_ID in result.stderr
    assert MARKER not in result.stdout + result.stderr


def test_an_ip_address_is_not_an_rp_id() -> None:
    result = _check({**LOCAL_DEBUG, RP_ID: "127.0.0.1"})

    assert result.returncode != 0
    assert RP_ID in result.stderr
    assert "127.0.0.1" not in result.stdout + result.stderr


def test_allowed_origins_default_to_the_frontend_url() -> None:
    values = {**PRODUCTION_LIKE, ORIGINS: ""}

    assert _webauthn(values)[1] == [PRODUCTION_LIKE["FRONTEND_URL"]]


def test_allowed_origins_are_read_from_the_environment() -> None:
    values = {
        **PRODUCTION_LIKE,
        ORIGINS: "https://app.example.org, https://www.example.org",
    }

    assert _webauthn(values)[1] == [
        "https://app.example.org",
        "https://www.example.org",
    ]


def test_allowed_origins_must_be_https_with_debug_off() -> None:
    result = _check({**PRODUCTION_LIKE, ORIGINS: f"http://{MARKER}.example.org"})

    assert result.returncode != 0
    assert ORIGINS in result.stderr
    assert "https" in result.stderr
    assert MARKER not in result.stdout + result.stderr


def test_a_bad_allowed_origin_stops_startup_without_echoing_it() -> None:
    result = _check({**LOCAL_DEBUG, ORIGINS: f"http://{MARKER}.example.org/path"})

    assert result.returncode != 0
    assert ORIGINS in result.stderr
    assert MARKER not in result.stdout + result.stderr


def test_debug_on_allows_an_http_localhost_origin() -> None:
    values = {**LOCAL_DEBUG, RP_ID: "localhost", ORIGINS: "http://localhost:8081"}

    assert _webauthn(values) == ["localhost", ["http://localhost:8081"]]


def test_the_deploy_check_warns_on_an_origin_outside_the_rp_id() -> None:
    values = {**PRODUCTION_LIKE, RP_ID: "openpb.org"}

    result = _check(values, "--deploy", "--fail-level", "WARNING")

    assert result.returncode != 0
    assert "accounts.W001" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("rp_id", "origins"),
    [
        ("example.org", ["https://example.org"]),
        ("example.org", ["https://app.example.org", "https://a.b.example.org"]),
        ("localhost", ["http://localhost:8081"]),
    ],
)
def test_the_check_accepts_origins_under_the_rp_id(
    settings: Any, rp_id: str, origins: list[str]
) -> None:
    settings.WEBAUTHN_RP_ID = rp_id
    settings.WEBAUTHN_ALLOWED_ORIGINS = origins

    assert check_webauthn_origins(None) == []


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.net",
        "https://badexample.org",
        "https://example.org.evil.test",
    ],
)
def test_the_check_warns_on_an_origin_outside_the_rp_id(
    settings: Any, origin: str
) -> None:
    settings.WEBAUTHN_RP_ID = "example.org"
    settings.WEBAUTHN_ALLOWED_ORIGINS = ["https://app.example.org", origin]

    [warning] = check_webauthn_origins(None)

    assert warning.id == "accounts.W001"
    assert origin not in warning.msg
    assert RP_ID in warning.msg
    assert ORIGINS in warning.msg

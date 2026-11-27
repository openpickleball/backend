"""OPEN_PUBLIC_PROFILE_URL: where a player's public profile lives (issue #53).

The rating lookup returns ``profile_url``, this setting followed by
the player's OPEN Code. Unset or empty, it is
``https://app.openpb.org/players/``, in the PWA. A value must be an
absolute http or https URL with a host and a path that ends in ``/``,
with no query, fragment, or user info, and https when DEBUG is off. A refused value stops startup,
naming the variable and never echoing the value. Each case runs in a
fresh process. Made-up hosts only.
"""

import json
import subprocess
import sys

import pytest

from config.tests.test_settings import BASE_DIR, LOCAL_DEBUG, PRODUCTION_LIKE, _env

NAME = "OPEN_PUBLIC_PROFILE_URL"
DEFAULT = "https://app.openpb.org/players/"

# A made-up host that must never appear in any output.
MARKER = "profile-q7z.example.org"

PRINT_PROFILE_URL = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps(settings.OPEN_PUBLIC_PROFILE_URL))
"""


def _run(
    args: list[str], values: dict[str, str], value: str
) -> subprocess.CompletedProcess[str]:
    env = _env(values)
    # Always set, so a developer's .env never supplies it.
    env[NAME] = value
    return subprocess.run(
        [sys.executable, *args],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _loaded(values: dict[str, str], value: str) -> str:
    result = _run(["-c", PRINT_PROFILE_URL], values, value)
    assert result.returncode == 0, result.stderr
    loaded: str = json.loads(result.stdout)
    return loaded


@pytest.mark.parametrize("values", [PRODUCTION_LIKE, LOCAL_DEBUG])
@pytest.mark.parametrize("value", ["", "   "])
def test_unset_or_empty_is_the_default(values: dict[str, str], value: str) -> None:
    assert _loaded(values, value) == DEFAULT


@pytest.mark.parametrize(
    "value",
    [
        "https://players.example.org/",
        "https://players.example.org/p/",
        "https://players.example.org:8443/open/players/",
    ],
)
def test_https_url_with_debug_off_loads(value: str) -> None:
    assert _loaded(PRODUCTION_LIKE, value) == value


def test_value_is_stripped() -> None:
    assert (
        _loaded(PRODUCTION_LIKE, "  https://players.example.org/p/  ")
        == "https://players.example.org/p/"
    )


@pytest.mark.parametrize(
    "value", ["http://localhost:8081/players/", "https://localhost:8443/players/"]
)
def test_debug_on_allows_http_and_https(value: str) -> None:
    assert _loaded(LOCAL_DEBUG, value) == value


@pytest.mark.parametrize(
    "value", [f"http://{MARKER}/players/", f"http://{MARKER}:8081/players/"]
)
def test_http_with_debug_off_stops_startup_without_echoing_it(value: str) -> None:
    result = _run(["manage.py", "check"], PRODUCTION_LIKE, value)

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert NAME in result.stderr
    assert "https" in result.stderr
    assert MARKER not in result.stdout + result.stderr


@pytest.mark.parametrize("values", [PRODUCTION_LIKE, LOCAL_DEBUG])
@pytest.mark.parametrize(
    "value",
    [
        # No trailing slash on the path.
        f"https://{MARKER}/players",
        f"https://{MARKER}",
        # Query, fragment, or user info.
        f"https://{MARKER}/players/?a=1",
        f"https://{MARKER}/players/?",
        f"https://{MARKER}/players/#top",
        f"https://{MARKER}/players/#",
        f"https://user:pass@{MARKER}/players/",
        f"https://user@{MARKER}/players/",
        # Not an absolute http or https URL with a host.
        f"ftp://{MARKER}/players/",
        f"HTTPS://{MARKER}/players/",
        f"//{MARKER}/players/",
        f"{MARKER}/players/",
        "/players/",
        "https:///players/",
        # A bad port, a malformed host, or a space or control character.
        f"https://{MARKER}:0/players/",
        f"https://{MARKER}:99999/players/",
        f"https://{MARKER}:abc/players/",
        f"https://[{MARKER}/players/",
        f"https://{MARKER}/play ers/",
        f"https://{MARKER}/players/\t/",
        f"https://{MARKER}/play\\ers/",
    ],
)
def test_refused_forms_stop_startup_without_echoing_them(
    values: dict[str, str], value: str
) -> None:
    result = _run(["manage.py", "check"], values, value)

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert NAME in result.stderr
    assert MARKER not in result.stdout + result.stderr

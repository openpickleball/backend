"""The API throttle settings (issue #34).

Each rate is a whole number from 1 to 1,000,000, with a default when unset
or empty. ``CLIENT_IP_HEADER`` names the request header that holds the
client IP: letters, digits, and hyphens, lowercased, never a header a
client can forge through the proxy. A bad value stops startup, naming the
variable and never echoing the value. Settings are read once at import, so
each case starts a fresh Python process.
"""

import json
import subprocess
import sys

import pytest

from config.tests.test_settings import BASE_DIR, LOCAL_DEBUG, _env

RATES = {
    "API_ACCOUNT_THROTTLE_PER_MINUTE": 120,
    "API_ACCOUNT_THROTTLE_PER_DAY": 10_000,
    "API_CLIENT_IP_THROTTLE_PER_MINUTE": 60,
    "API_CLIENT_IP_THROTTLE_PER_DAY": 3_000,
    "API_ORGANIZER_REPORT_THROTTLE_PER_DAY": 5,
}
HEADER = "CLIENT_IP_HEADER"
THROTTLE_VARS = (*RATES, HEADER)

PRINT_THROTTLE_SETTINGS = f"""
import json
import django
from django.conf import settings
django.setup()
print(json.dumps({{name: getattr(settings, name) for name in {THROTTLE_VARS!r}}}))
"""


def _run(args: list[str], values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run with every throttle variable set ('' means absent)."""
    env = _env(LOCAL_DEBUG)
    for name in THROTTLE_VARS:
        env[name] = values.get(name, "")
    return subprocess.run(
        [sys.executable, *args],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _throttle_settings(values: dict[str, str]) -> dict[str, object]:
    result = _run(["-c", PRINT_THROTTLE_SETTINGS], values)
    assert result.returncode == 0, result.stderr
    loaded: dict[str, object] = json.loads(result.stdout)
    return loaded


def _assert_refused(name: str, value: str) -> None:
    result = _run(["manage.py", "check"], {name: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    assert f"={value}" not in result.stderr
    assert "MARKER" not in result.stderr
    assert "MARKER" not in result.stdout


def test_defaults_when_unset() -> None:
    assert _throttle_settings({}) == {**RATES, HEADER: "do-connecting-ip"}


@pytest.mark.parametrize("value", ["", "  "])
def test_empty_values_use_the_defaults(value: str) -> None:
    loaded = _throttle_settings(dict.fromkeys(THROTTLE_VARS, value))

    assert loaded == {**RATES, HEADER: "do-connecting-ip"}


@pytest.mark.parametrize("name", list(RATES))
@pytest.mark.parametrize(
    ("value", "rate"), [("1", 1), (" 45 ", 45), ("1000000", 1_000_000)]
)
def test_a_rate_is_a_whole_number(name: str, value: str, rate: int) -> None:
    assert _throttle_settings({name: value})[name] == rate


@pytest.mark.parametrize("name", list(RATES))
@pytest.mark.parametrize(
    "value",
    ["0", "-3", "1000001", "99999999", "1.5", "abc", "60/m", "٣", "ten-MARKER"],
)
def test_a_bad_rate_stops_startup_without_echoing_it(name: str, value: str) -> None:
    _assert_refused(name, value)


@pytest.mark.parametrize(
    ("value", "header"),
    [
        ("do-connecting-ip", "do-connecting-ip"),
        (" CF-Connecting-IP ", "cf-connecting-ip"),
        ("x-real-ip", "x-real-ip"),
        ("a" * 64, "a" * 64),
    ],
)
def test_client_ip_header_is_stripped_and_lowercased(value: str, header: str) -> None:
    assert _throttle_settings({HEADER: value})[HEADER] == header


@pytest.mark.parametrize(
    "value",
    [
        "x-forwarded-for",
        "X-Forwarded-For",
        "forwarded",
        "Forwarded",
        "do_connecting_ip",
        "do connecting ip",
        "x-real-ip,x-forwarded-for",
        "a" * 65,
        "é-ip",
        "MARKER:ip",
    ],
)
def test_a_bad_client_ip_header_stops_startup_without_echoing_it(value: str) -> None:
    _assert_refused(HEADER, value)

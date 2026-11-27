"""The unusual submission thresholds (#206, #207).

Each is a whole number from 1 to 1,000,000, with a default when unset or
empty. A bad value stops startup, naming the variable, never echoing the
value. Settings are read once at import, so each case starts a fresh
Python process.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, _run

DEFAULTS = {
    "UNUSUAL_SUBMISSION_FIRST_MATCHES": 25,
    "UNUSUAL_SUBMISSION_BATCH_SIZE": 100,
    "UNUSUAL_SUBMISSION_DAILY_MATCHES": 200,
    "UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES": 30,
}
PRINT_THRESHOLDS = f"""
import json
import django
from django.conf import settings
django.setup()
print(json.dumps({{name: getattr(settings, name) for name in {tuple(DEFAULTS)!r}}}))
"""


def _thresholds(env: dict[str, str]) -> dict[str, int]:
    result = _run(["-c", PRINT_THRESHOLDS], {**LOCAL_DEBUG, **env})
    assert result.returncode == 0, result.stderr
    values: dict[str, int] = json.loads(result.stdout)
    return values


def test_unset_gives_the_defaults() -> None:
    assert _thresholds({}) == DEFAULTS


def test_empty_gives_the_defaults() -> None:
    assert _thresholds({name: "  " for name in DEFAULTS}) == DEFAULTS


@pytest.mark.parametrize("name", list(DEFAULTS))
@pytest.mark.parametrize(
    ("value", "number"), [("1", 1), (" 7 ", 7), ("1000000", 1_000_000)]
)
def test_a_whole_number(name: str, value: str, number: int) -> None:
    assert _thresholds({name: value})[name] == number


@pytest.mark.parametrize("name", list(DEFAULTS))
@pytest.mark.parametrize("value", ["0", "-3", "1000001", "1.5", "seven-MARKER", "٣"])
def test_a_bad_value_stops_startup_without_echoing_it(name: str, value: str) -> None:
    result = _run(["manage.py", "check"], {**LOCAL_DEBUG, name: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    assert f"={value}" not in result.stderr
    assert "MARKER" not in result.stderr
    assert "MARKER" not in result.stdout

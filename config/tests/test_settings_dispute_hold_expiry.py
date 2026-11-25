"""MATCH_DISPUTE_EXPIRY_DAYS and MATCH_DUPLICATE_HOLD_DAYS (#268, ADR 0034).

Each is a whole number of days from 1 to 36500. Unset or empty gives the
default (30 and 14): there is no "never" value. A bad value stops
startup, naming the variable, never echoing the value.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, _run

PRINT_DAYS = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps([settings.MATCH_DISPUTE_EXPIRY_DAYS, settings.MATCH_DUPLICATE_HOLD_DAYS]))
"""

DISPUTE = "MATCH_DISPUTE_EXPIRY_DAYS"
HOLD = "MATCH_DUPLICATE_HOLD_DAYS"


def _days(env: dict[str, str]) -> list[int]:
    result = _run(["-c", PRINT_DAYS], {**LOCAL_DEBUG, **env})
    assert result.returncode == 0, result.stderr
    days: list[int] = json.loads(result.stdout)
    return days


@pytest.mark.parametrize("value", ["", "  "])
def test_empty_gives_the_defaults(value: str) -> None:
    assert _days({DISPUTE: value, HOLD: value}) == [30, 14]


@pytest.mark.parametrize(("value", "days"), [("1", 1), (" 45 ", 45), ("36500", 36500)])
def test_a_whole_number_of_days(value: str, days: int) -> None:
    assert _days({DISPUTE: value, HOLD: value}) == [days, days]


@pytest.mark.parametrize("name", [DISPUTE, HOLD])
@pytest.mark.parametrize(
    "value", ["0", "-3", "36501", "7days", "1.5", "seven-MARKER", "٣", "9" * 5000]
)
def test_a_bad_value_stops_startup_without_echoing_it(name: str, value: str) -> None:
    result = _run(["manage.py", "check"], {**LOCAL_DEBUG, name: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    assert f"={value}" not in result.stderr
    if len(value) > 3:
        assert value not in result.stderr
    assert "MARKER" not in result.stderr
    assert "MARKER" not in result.stdout

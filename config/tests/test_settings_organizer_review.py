"""ORGANIZER_REVIEW_WINDOW_DAYS (#271).

A whole number of days from 1 to 36500. Unset or empty gives 90. A bad
value stops startup, naming the variable, never echoing the value.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, _run

NAME = "ORGANIZER_REVIEW_WINDOW_DAYS"
PRINT_DAYS = f"""
import json
import django
from django.conf import settings
django.setup()
print(json.dumps(settings.{NAME}))
"""


def _days(env: dict[str, str]) -> int:
    result = _run(["-c", PRINT_DAYS], {**LOCAL_DEBUG, **env})
    assert result.returncode == 0, result.stderr
    days: int = json.loads(result.stdout)
    return days


@pytest.mark.parametrize("value", ["", "  "])
def test_empty_gives_the_default(value: str) -> None:
    assert _days({NAME: value}) == 90


def test_unset_gives_the_default() -> None:
    assert _days({}) == 90


@pytest.mark.parametrize(("value", "days"), [("1", 1), (" 45 ", 45), ("36500", 36500)])
def test_a_whole_number_of_days(value: str, days: int) -> None:
    assert _days({NAME: value}) == days


@pytest.mark.parametrize(
    "value", ["0", "-3", "36501", "7days", "1.5", "seven-MARKER", "٣"]
)
def test_a_bad_value_stops_startup_without_echoing_it(value: str) -> None:
    result = _run(["manage.py", "check"], {**LOCAL_DEBUG, NAME: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert NAME in result.stderr
    assert f"={value}" not in result.stderr
    assert "MARKER" not in result.stderr
    assert "MARKER" not in result.stdout

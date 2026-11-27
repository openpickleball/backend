"""Club self-service settings (#230).

``CLUB_CREATE_LIMIT_PER_ACCOUNT`` and ``CLUB_OPEN_INVITES_LIMIT`` are
whole numbers from 1 to 1,000,000 and ``CLUB_INVITE_DAYS`` a whole number
of days from 1 to 36500. Unset or empty gives 5, 50, and 7. A bad value
stops startup, naming the variable, never echoing the value.
``API_CLUB_INVITE_THROTTLE_PER_DAY`` is checked with the other rates in
``test_settings_throttle.py``.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, _run

DEFAULTS = {
    "CLUB_CREATE_LIMIT_PER_ACCOUNT": 5,
    "CLUB_OPEN_INVITES_LIMIT": 50,
    "CLUB_INVITE_DAYS": 7,
}
PRINT = f"""
import json
import django
from django.conf import settings
django.setup()
print(json.dumps({{name: getattr(settings, name) for name in {tuple(DEFAULTS)!r}}}))
"""


def _loaded(env: dict[str, str]) -> dict[str, int]:
    result = _run(["-c", PRINT], {**LOCAL_DEBUG, **env})
    assert result.returncode == 0, result.stderr
    loaded: dict[str, int] = json.loads(result.stdout)
    return loaded


def test_unset_gives_the_defaults() -> None:
    assert _loaded({}) == DEFAULTS


@pytest.mark.parametrize("value", ["", "  "])
def test_empty_gives_the_defaults(value: str) -> None:
    assert _loaded(dict.fromkeys(DEFAULTS, value)) == DEFAULTS


def test_whole_numbers_are_read() -> None:
    assert _loaded(
        {
            "CLUB_CREATE_LIMIT_PER_ACCOUNT": "2",
            "CLUB_OPEN_INVITES_LIMIT": " 9 ",
            "CLUB_INVITE_DAYS": "3",
        }
    ) == {
        "CLUB_CREATE_LIMIT_PER_ACCOUNT": 2,
        "CLUB_OPEN_INVITES_LIMIT": 9,
        "CLUB_INVITE_DAYS": 3,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CLUB_CREATE_LIMIT_PER_ACCOUNT", "0"),
        ("CLUB_OPEN_INVITES_LIMIT", "five-MARKER"),
        ("CLUB_INVITE_DAYS", "36501"),
    ],
)
def test_a_bad_value_stops_startup_without_echoing_it(name: str, value: str) -> None:
    result = _run(["manage.py", "check"], {**LOCAL_DEBUG, name: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    assert f"={value}" not in result.stderr
    assert "MARKER" not in result.stderr
    assert "MARKER" not in result.stdout

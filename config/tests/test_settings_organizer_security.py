"""Organizer and Club admin security settings (#232, ADR 0057).

``ORGANIZER_STEP_UP_SECONDS`` is whole seconds from 30 to 86400 (default
300). ``ORGANIZER_NEW_MEMBER_HOLD_HOURS`` is whole hours from 0 to 8760
(default 24; 0 turns the hold off). ``ORGANIZER_SESSION_MAX_AGE_DAYS``
(default 7, or ``SESSION_MAX_AGE_DAYS`` when lower) and
``ORGANIZER_SESSION_IDLE_DAYS`` (default 1) are whole days, each at most
its player value, and the idle time at most the lifetime. A bad value
stops startup, naming the variable, never echoing the value.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, _run

DEFAULTS = {
    "ORGANIZER_STEP_UP_SECONDS": 300,
    "ORGANIZER_NEW_MEMBER_HOLD_HOURS": 24,
    "ORGANIZER_SESSION_MAX_AGE_DAYS": 7,
    "ORGANIZER_SESSION_IDLE_DAYS": 1,
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
            "ORGANIZER_STEP_UP_SECONDS": "30",
            "ORGANIZER_NEW_MEMBER_HOLD_HOURS": " 0 ",
            "ORGANIZER_SESSION_MAX_AGE_DAYS": "3",
            "ORGANIZER_SESSION_IDLE_DAYS": "2",
        }
    ) == {
        "ORGANIZER_STEP_UP_SECONDS": 30,
        "ORGANIZER_NEW_MEMBER_HOLD_HOURS": 0,
        "ORGANIZER_SESSION_MAX_AGE_DAYS": 3,
        "ORGANIZER_SESSION_IDLE_DAYS": 2,
    }


def test_a_short_player_lifetime_lowers_the_default_member_lifetime() -> None:
    loaded = _loaded({"SESSION_MAX_AGE_DAYS": "5", "SESSION_IDLE_DAYS": "2"})

    assert loaded["ORGANIZER_SESSION_MAX_AGE_DAYS"] == 5
    assert loaded["ORGANIZER_SESSION_IDLE_DAYS"] == 1


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({"ORGANIZER_STEP_UP_SECONDS": "29"}, "ORGANIZER_STEP_UP_SECONDS"),
        ({"ORGANIZER_STEP_UP_SECONDS": "86401"}, "ORGANIZER_STEP_UP_SECONDS"),
        (
            {"ORGANIZER_NEW_MEMBER_HOLD_HOURS": "8761"},
            "ORGANIZER_NEW_MEMBER_HOLD_HOURS",
        ),
        (
            {"ORGANIZER_NEW_MEMBER_HOLD_HOURS": "one-MARKER"},
            "ORGANIZER_NEW_MEMBER_HOLD_HOURS",
        ),
        (
            {"ORGANIZER_NEW_MEMBER_HOLD_HOURS": "-1"},
            "ORGANIZER_NEW_MEMBER_HOLD_HOURS",
        ),
        ({"ORGANIZER_SESSION_MAX_AGE_DAYS": "0"}, "ORGANIZER_SESSION_MAX_AGE_DAYS"),
        ({"ORGANIZER_SESSION_MAX_AGE_DAYS": "31"}, "ORGANIZER_SESSION_MAX_AGE_DAYS"),
        ({"ORGANIZER_SESSION_IDLE_DAYS": "8"}, "ORGANIZER_SESSION_IDLE_DAYS"),
        (
            {"ORGANIZER_SESSION_MAX_AGE_DAYS": "2", "ORGANIZER_SESSION_IDLE_DAYS": "3"},
            "ORGANIZER_SESSION_IDLE_DAYS",
        ),
    ],
)
def test_a_bad_value_stops_startup_without_echoing_it(
    env: dict[str, str], name: str
) -> None:
    result = _run(["manage.py", "check"], {**LOCAL_DEBUG, **env})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    for value in env.values():
        assert f"={value}" not in result.stderr
    assert "MARKER" not in result.stderr
    assert "MARKER" not in result.stdout

"""ADMIN_SESSION_SECONDS has a floor of 30, and an unset reauthentication
window follows it (issue #101).

Reauthentication needs at least 30 seconds and at most the session, so a
session under 30 could never be valid, and a session under 300 with
ADMIN_REAUTHENTICATION_SECONDS unset failed on the 300 default. Unset or
empty, the reauthentication window is now min(300, the session). A
refused value stops startup, naming the variable.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, _run

SESSION = "ADMIN_SESSION_SECONDS"
REAUTH = "ADMIN_REAUTHENTICATION_SECONDS"
PRINT = f"""
import json
import django
from django.conf import settings
django.setup()
print(json.dumps([settings.{SESSION}, settings.{REAUTH}]))
"""


def _loaded(env: dict[str, str]) -> list[int]:
    result = _run(["-c", PRINT], {**LOCAL_DEBUG, **env})
    assert result.returncode == 0, result.stderr
    loaded: list[int] = json.loads(result.stdout)
    return loaded


@pytest.mark.parametrize("value", ["1", "10", "29"])
def test_a_session_under_thirty_stops_startup(value: str) -> None:
    result = _run(["manage.py", "check"], {**LOCAL_DEBUG, SESSION: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    # The session's own error, not the reauthentication ceiling's.
    assert f"Set {SESSION} to a whole number of seconds from 30" in result.stderr


@pytest.mark.parametrize(
    ("session", "expected"),
    [("", [3600, 300]), ("120", [120, 120]), ("30", [30, 30]), ("600", [600, 300])],
)
def test_an_unset_reauthentication_follows_the_session(
    session: str, expected: list[int]
) -> None:
    assert _loaded({SESSION: session, REAUTH: ""}) == expected


def test_a_set_reauthentication_is_kept() -> None:
    assert _loaded({SESSION: "120", REAUTH: "45"}) == [120, 45]


def test_a_set_reauthentication_over_the_session_stops_startup() -> None:
    result = _run(
        ["manage.py", "check"], {**LOCAL_DEBUG, SESSION: "120", REAUTH: "200"}
    )

    assert result.returncode != 0
    assert REAUTH in result.stderr

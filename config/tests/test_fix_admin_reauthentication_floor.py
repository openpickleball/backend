"""ADMIN_REAUTHENTICATION_SECONDS has a floor of 30 (issue #101).

A few seconds would send nearly every admin write back for a code. The
ceiling stays ADMIN_SESSION_SECONDS. A refused value stops startup,
naming the variable, never echoing the value.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, _run

NAME = "ADMIN_REAUTHENTICATION_SECONDS"
PRINT = f"""
import json
import django
from django.conf import settings
django.setup()
print(json.dumps(settings.{NAME}))
"""


@pytest.mark.parametrize("value", ["1", "5", "29"])
def test_a_value_under_thirty_stops_startup(value: str) -> None:
    result = _run(["manage.py", "check"], {**LOCAL_DEBUG, NAME: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert NAME in result.stderr


@pytest.mark.parametrize(("value", "seconds"), [("30", 30), (" 31 ", 31)])
def test_thirty_and_more_are_accepted(value: str, seconds: int) -> None:
    result = _run(["-c", PRINT], {**LOCAL_DEBUG, NAME: value})

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == seconds


def test_the_ceiling_is_still_the_session_limit() -> None:
    result = _run(
        ["manage.py", "check"],
        {**LOCAL_DEBUG, NAME: "61", "ADMIN_SESSION_SECONDS": "60"},
    )

    assert result.returncode != 0
    assert NAME in result.stderr

"""CSP_REPORT_ONLY accepts only true and false spellings (issue #102).

An unrecognized value stops startup, naming the variable without echoing
the value, instead of silently picking a mode.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _check, _run

# A made-up value that must never appear in any output.
MARKER = "csp-marker-v8q"

PRINT_MODE = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps(settings.CONTENT_SECURITY_POLICY_REPORT_ONLY is not None))
"""


@pytest.mark.parametrize("value", ["ture", "report", "enforce", "2", MARKER])
@pytest.mark.parametrize("env", [PRODUCTION_LIKE, LOCAL_DEBUG], ids=["off", "on"])
def test_an_unrecognized_value_stops_startup(env: dict[str, str], value: str) -> None:
    result = _check({**env, "CSP_REPORT_ONLY": value})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "CSP_REPORT_ONLY" in result.stderr
    assert MARKER not in result.stderr
    assert MARKER not in result.stdout


@pytest.mark.parametrize(
    ("value", "report_only"),
    [
        ("true", True),
        (" TRUE ", True),
        ("Yes", True),
        ("on", True),
        ("1", True),
        ("false", False),
        (" False ", False),
        ("NO", False),
        ("off", False),
        ("0", False),
    ],
)
def test_true_and_false_spellings_are_accepted(
    value: str, *, report_only: bool
) -> None:
    for env in (PRODUCTION_LIKE, LOCAL_DEBUG):
        result = _run(["-c", PRINT_MODE], {**env, "CSP_REPORT_ONLY": value})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) is report_only


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_follows_debug(value: str) -> None:
    for env, report_only in ((PRODUCTION_LIKE, False), (LOCAL_DEBUG, True)):
        result = _run(["-c", PRINT_MODE], {**env, "CSP_REPORT_ONLY": value})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) is report_only

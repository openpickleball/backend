"""The account security settings (#100, ADR 0047).

Each comes from the environment with a default, and a bad value stops
startup, naming the variable, never echoing the value.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _run

PRINT = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps({
    'PASSWORD_MIN_LENGTH': settings.PASSWORD_MIN_LENGTH,
    'MIN_LENGTH_OPTION': settings.AUTH_PASSWORD_VALIDATORS[1]['OPTIONS']['min_length'],
    'PASSWORD_BREACH_API_URL': settings.PASSWORD_BREACH_API_URL,
    'PASSWORD_BREACH_TIMEOUT_SECONDS': settings.PASSWORD_BREACH_TIMEOUT_SECONDS,
    'SESSION_MAX_AGE_DAYS': settings.SESSION_MAX_AGE_DAYS,
    'SESSION_IDLE_DAYS': settings.SESSION_IDLE_DAYS,
    'SESSION_COOKIE_AGE': settings.SESSION_COOKIE_AGE,
    'SIGN_IN_DEVICE_COOKIE_DAYS': settings.SIGN_IN_DEVICE_COOKIE_DAYS,
    'SIGN_IN_DEVICE_COOKIE_NAME': settings.SIGN_IN_DEVICE_COOKIE_NAME,
    'SIGN_IN_DEVICE_COOKIE_SECURE': settings.SIGN_IN_DEVICE_COOKIE_SECURE,
}))
"""

DAY = 86400


def _settings(
    env: dict[str, str], base: dict[str, str] = LOCAL_DEBUG
) -> dict[str, object]:
    result = _run(["-c", PRINT], {**base, **env})
    assert result.returncode == 0, result.stderr
    values: dict[str, object] = json.loads(result.stdout)
    return values


def _refused(env: dict[str, str], name: str, value: str) -> None:
    result = _run(["manage.py", "check"], {**LOCAL_DEBUG, **env})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    assert f"={value}" not in result.stderr
    assert "MARKER" not in result.stderr


def test_the_defaults() -> None:
    values = _settings({})

    assert values == {
        "PASSWORD_MIN_LENGTH": 8,
        "MIN_LENGTH_OPTION": 8,
        "PASSWORD_BREACH_API_URL": "https://api.pwnedpasswords.com/range/",
        "PASSWORD_BREACH_TIMEOUT_SECONDS": 3,
        "SESSION_MAX_AGE_DAYS": 30,
        "SESSION_IDLE_DAYS": 7,
        "SESSION_COOKIE_AGE": 14 * DAY,
        "SIGN_IN_DEVICE_COOKIE_DAYS": 365,
        "SIGN_IN_DEVICE_COOKIE_NAME": "open_device",
        "SIGN_IN_DEVICE_COOKIE_SECURE": False,
    }


def test_production_names_the_device_cookie_with_the_host_prefix() -> None:
    values = _settings({}, base=PRODUCTION_LIKE)

    assert values["SIGN_IN_DEVICE_COOKIE_NAME"] == "__Host-open_device"
    assert values["SIGN_IN_DEVICE_COOKIE_SECURE"] is True


def test_values_from_the_environment() -> None:
    values = _settings(
        {
            "PASSWORD_MIN_LENGTH": "12",
            "PASSWORD_BREACH_TIMEOUT_SECONDS": "5",
            "SESSION_MAX_AGE_DAYS": "14",
            "SESSION_IDLE_DAYS": "14",
            "SIGN_IN_DEVICE_COOKIE_DAYS": "90",
        }
    )

    assert values["PASSWORD_MIN_LENGTH"] == values["MIN_LENGTH_OPTION"] == 12
    assert values["PASSWORD_BREACH_TIMEOUT_SECONDS"] == 5
    assert values["SESSION_MAX_AGE_DAYS"] == values["SESSION_IDLE_DAYS"] == 14
    # Signed-out sessions keep two weeks, whatever the lifetime.
    assert values["SESSION_COOKIE_AGE"] == 14 * DAY
    assert values["SIGN_IN_DEVICE_COOKIE_DAYS"] == 90


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PASSWORD_MIN_LENGTH", "7"),
        ("PASSWORD_MIN_LENGTH", "129"),
        ("PASSWORD_MIN_LENGTH", "eight-MARKER"),
        ("PASSWORD_BREACH_TIMEOUT_SECONDS", "0"),
        ("PASSWORD_BREACH_TIMEOUT_SECONDS", "11"),
        ("SESSION_MAX_AGE_DAYS", "0"),
        ("SESSION_IDLE_DAYS", "1.5"),
        ("SIGN_IN_DEVICE_COOKIE_DAYS", "36501"),
    ],
)
def test_a_bad_value_stops_startup_without_echoing_it(name: str, value: str) -> None:
    _refused({name: value}, name, value)


def test_an_idle_expiry_longer_than_the_lifetime_stops_startup() -> None:
    result = _run(
        ["manage.py", "check"],
        {**LOCAL_DEBUG, "SESSION_MAX_AGE_DAYS": "5", "SESSION_IDLE_DAYS": "6"},
    )

    assert result.returncode != 0
    assert "SESSION_IDLE_DAYS" in result.stderr
    assert "SESSION_MAX_AGE_DAYS" in result.stderr


def test_the_breach_service_is_not_read_from_the_environment() -> None:
    env = {"PASSWORD_BREACH_API_URL": "https://elsewhere.example.org/range/"}
    result = _run(["-c", PRINT], {**LOCAL_DEBUG, **env})

    assert result.returncode == 0, result.stderr
    assert "elsewhere" not in result.stdout

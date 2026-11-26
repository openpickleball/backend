"""The FIELD_ENCRYPTION_KEYS setting (issue #115, ADR 0046).

A comma-separated list of Fernet keys, newest first. With DEBUG off it
is required. Every entry must be a Fernet key (urlsafe base64 of 32
bytes), in any mode. A bad value stops startup, naming the variable and
the entry's position, never echoing a value. Keys are generated at test
time: none is committed.
"""

import json

import pytest
from cryptography.fernet import Fernet

from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _run

PRINT_KEYS = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps(settings.FIELD_ENCRYPTION_KEYS))
"""
MARKER = "MARKERx9"


def _key() -> str:
    return Fernet.generate_key().decode()


def _keys(env: dict[str, str]) -> list[str]:
    result = _run(["-c", PRINT_KEYS], env)
    assert result.returncode == 0, result.stderr
    loaded: list[str] = json.loads(result.stdout)
    return loaded


def _refused(env: dict[str, str]) -> str:
    result = _run(["manage.py", "check"], env)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "FIELD_ENCRYPTION_KEYS" in result.stderr
    return result.stderr + result.stdout


def test_keys_are_listed_newest_first() -> None:
    new, old = _key(), _key()
    for env in (LOCAL_DEBUG, PRODUCTION_LIKE):
        value = f" {new} , {old} ,"
        assert _keys({**env, "FIELD_ENCRYPTION_KEYS": value}) == [new, old]


@pytest.mark.parametrize("value", ["", " ", " , "])
def test_debug_off_requires_a_key(value: str) -> None:
    output = _refused({**PRODUCTION_LIKE, "FIELD_ENCRYPTION_KEYS": value})
    assert "DJANGO_DEBUG" in output


@pytest.mark.parametrize("value", ["", " "])
def test_debug_on_may_leave_it_empty(value: str) -> None:
    assert _keys({**LOCAL_DEBUG, "FIELD_ENCRYPTION_KEYS": value}) == []


@pytest.mark.parametrize(
    "bad",
    [
        MARKER,
        MARKER + "A" * 36,
        # Base64 of 31 bytes, padded to 44 characters.
        MARKER + "A" * 34 + "==",
        # A 44 character value that is not urlsafe base64.
        MARKER + "+/" * 17 + "=",
        # Key-shaped, with one character outside base64.
        "*" + "A" * 42 + "=",
    ],
)
def test_a_malformed_entry_is_refused_in_any_mode(bad: str) -> None:
    for env in (LOCAL_DEBUG, PRODUCTION_LIKE):
        output = _refused({**env, "FIELD_ENCRYPTION_KEYS": f"{_key()},{bad}"})
        assert "entry 2" in output
        assert bad not in output
        assert MARKER not in output


def test_the_first_bad_entry_is_named() -> None:
    output = _refused({**LOCAL_DEBUG, "FIELD_ENCRYPTION_KEYS": f"{MARKER},{_key()}"})
    assert "entry 1" in output
    assert MARKER not in output

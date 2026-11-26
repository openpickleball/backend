"""The admin settings (issue #101).

``ADMIN_URL`` is the admin's path: one or more segments of letters,
digits, ``-``, and ``_``, each ending in ``/``, no leading slash, at most
100 characters, whose first segment is not ``api``, ``_allauth``,
``oauth``, or ``static``. With DEBUG on and the variable unset or empty
it is ``admin/``. With DEBUG off it is required and ``admin/`` is
refused, whatever its case.

``ADMIN_SESSION_SECONDS`` (default 3600) is how long an MFA step opens
the admin, and ``ADMIN_REAUTHENTICATION_SECONDS`` (default 300) how
recent one must be before a write. Each is a whole number of seconds from
1 to 86400, reauthentication at least 30 and at most the session limit.

A bad value stops startup, naming the variable, never echoing the value.
Settings are read once at import, so each case starts a fresh process.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _run

NAMES = ("ADMIN_URL", "ADMIN_SESSION_SECONDS", "ADMIN_REAUTHENTICATION_SECONDS")
PRINT_ADMIN = f"""
import json
import django
from django.conf import settings
django.setup()
print(json.dumps({{name: getattr(settings, name) for name in {NAMES!r}}}))
"""
MARKER = "MARKERx9"


def _admin_settings(env: dict[str, str]) -> dict[str, object]:
    result = _run(["-c", PRINT_ADMIN], env)
    assert result.returncode == 0, result.stderr
    loaded: dict[str, object] = json.loads(result.stdout)
    return loaded


def _assert_refused(env: dict[str, str], name: str) -> None:
    result = _run(["manage.py", "check"], env)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    assert MARKER not in result.stderr
    assert MARKER not in result.stdout


@pytest.mark.parametrize("value", ["", "  "])
def test_debug_on_without_a_path_uses_admin(value: str) -> None:
    loaded = _admin_settings({**LOCAL_DEBUG, "ADMIN_URL": value})

    assert loaded == {
        "ADMIN_URL": "admin/",
        "ADMIN_SESSION_SECONDS": 3600,
        "ADMIN_REAUTHENTICATION_SECONDS": 300,
    }


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ("staff-x7/", "staff-x7/"),
        (" Staff_Area/inner-2/ ", "Staff_Area/inner-2/"),
        ("a/", "a/"),
        ("x" * 99 + "/", "x" * 99 + "/"),
        ("apis/", "apis/"),
    ],
)
def test_a_valid_path_is_used(value: str, path: str) -> None:
    for env in (LOCAL_DEBUG, PRODUCTION_LIKE):
        assert _admin_settings({**env, "ADMIN_URL": value})["ADMIN_URL"] == path


def test_debug_on_accepts_admin() -> None:
    loaded = _admin_settings({**LOCAL_DEBUG, "ADMIN_URL": "admin/"})

    assert loaded["ADMIN_URL"] == "admin/"


@pytest.mark.parametrize("value", ["", "  "])
def test_debug_off_requires_a_path(value: str) -> None:
    _assert_refused({**PRODUCTION_LIKE, "ADMIN_URL": value}, "ADMIN_URL")


@pytest.mark.parametrize("value", ["admin/", "Admin/", "ADMIN/", " admin/ "])
def test_debug_off_refuses_the_local_default(value: str) -> None:
    _assert_refused({**PRODUCTION_LIKE, "ADMIN_URL": value}, "ADMIN_URL")


@pytest.mark.parametrize(
    "value",
    [
        f"/{MARKER}/",
        f"{MARKER}",
        f"{MARKER}//",
        f"{MARKER}/x",
        f"{MARKER}.x/",
        f"{MARKER} x/",
        f"{MARKER}%2F/",
        f"{MARKER}é/",
        f"{MARKER}/../",
        "x" * 100 + f"{MARKER}/",
        "/",
    ],
)
def test_a_bad_shape_stops_startup_without_echoing_it(value: str) -> None:
    for env in (LOCAL_DEBUG, PRODUCTION_LIKE):
        _assert_refused({**env, "ADMIN_URL": value}, "ADMIN_URL")


@pytest.mark.parametrize(
    "value", ["api/", "api/x/", "_allauth/", "oauth/", "static/", "API/", "Static/x/"]
)
def test_a_reserved_first_segment_stops_startup(value: str) -> None:
    for env in (LOCAL_DEBUG, PRODUCTION_LIKE):
        _assert_refused({**env, "ADMIN_URL": value}, "ADMIN_URL")


@pytest.mark.parametrize(
    ("session", "reauth", "expected"),
    [
        ("7200", "600", (7200, 600)),
        (" 60 ", "60", (60, 60)),
        ("86400", "30", (86400, 30)),
        ("", "120", (3600, 120)),
    ],
)
def test_seconds_are_read(session: str, reauth: str, expected: tuple[int, int]) -> None:
    loaded = _admin_settings(
        {
            **LOCAL_DEBUG,
            "ADMIN_SESSION_SECONDS": session,
            "ADMIN_REAUTHENTICATION_SECONDS": reauth,
        }
    )

    assert (
        loaded["ADMIN_SESSION_SECONDS"],
        loaded["ADMIN_REAUTHENTICATION_SECONDS"],
    ) == expected


@pytest.mark.parametrize(
    "name", ["ADMIN_SESSION_SECONDS", "ADMIN_REAUTHENTICATION_SECONDS"]
)
@pytest.mark.parametrize("value", ["0", "-5", "86401", "1.5", f"9{MARKER}", "٣"])
def test_bad_seconds_stop_startup_without_echoing_them(name: str, value: str) -> None:
    _assert_refused({**LOCAL_DEBUG, name: value}, name)


def test_reauthentication_may_not_exceed_the_session() -> None:
    _assert_refused(
        {
            **LOCAL_DEBUG,
            "ADMIN_SESSION_SECONDS": "300",
            "ADMIN_REAUTHENTICATION_SECONDS": "301",
        },
        "ADMIN_REAUTHENTICATION_SECONDS",
    )

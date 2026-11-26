"""Tests for the pooled database settings (issue #60, ADR 0038).

DATABASE_URL may point at a PgBouncer pool in transaction mode. Then
DIRECT_DATABASE_URL names the database itself, for migrations. Each case
starts a fresh Python process with a fully controlled environment, like
config/tests/test_settings.py, whose helpers these reuse.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _check, _run

# A made-up password that must never appear in any output.
DB_MARKER = "not-a-real-db-pw-MARKER"

POOL_URL = f"postgres://open:{DB_MARKER}@pool.example.org:25061/open-pool"
DIRECT_URL = f"postgres://open:{DB_MARKER}@db.example.org:25060/open"

POOLED = {
    **PRODUCTION_LIKE,
    "DATABASE_URL": POOL_URL,
    "DIRECT_DATABASE_URL": DIRECT_URL,
}

# Prints each alias's settings and the parameters psycopg would connect
# with. Building the parameters does not connect.
PRINT_DATABASES = """
import json
import django
from django.conf import settings
from django.db import connections
django.setup()
out = {}
for alias in settings.DATABASES:
    # The connection's copy has Django's defaults filled in.
    db = connections[alias].settings_dict
    params = connections[alias].get_connection_params()
    out[alias] = {
        'NAME': db['NAME'],
        'HOST': db['HOST'],
        'PORT': str(db['PORT']),
        'CONN_MAX_AGE': db['CONN_MAX_AGE'],
        'DISABLE_SERVER_SIDE_CURSORS': db.get('DISABLE_SERVER_SIDE_CURSORS', False),
        'TEST_MIRROR': db['TEST'].get('MIRROR'),
        'OPTIONS': sorted(db['OPTIONS']),
        'HAS_PREPARE_THRESHOLD': 'prepare_threshold' in params,
        'PREPARE_THRESHOLD': params.get('prepare_threshold'),
    }
print(json.dumps(out))
"""


def _databases(values: dict[str, str]) -> dict[str, dict[str, object]]:
    result = _run(["-c", PRINT_DATABASES], values)
    if result.returncode != 0:
        raise AssertionError(f"settings failed to load:\n{result.stderr}")
    databases: dict[str, dict[str, object]] = json.loads(result.stdout)
    return databases


def test_without_direct_url_there_is_only_default() -> None:
    databases = _databases(LOCAL_DEBUG)
    assert list(databases) == ["default"]
    assert databases["default"]["HOST"] == "localhost"
    assert databases["default"]["TEST_MIRROR"] is None


def test_blank_direct_url_is_unpooled() -> None:
    databases = _databases({**LOCAL_DEBUG, "DIRECT_DATABASE_URL": "   "})
    assert list(databases) == ["default"]


def test_unpooled_keeps_the_url_options() -> None:
    # Outside pooled mode nothing changes: the URL's query still applies.
    url = "postgres://open@localhost:5432/open?conn_max_age=60"
    databases = _databases({**LOCAL_DEBUG, "DATABASE_URL": url})
    assert databases["default"]["CONN_MAX_AGE"] == 60


def test_pooled_mode_adds_the_direct_alias() -> None:
    databases = _databases(POOLED)
    assert sorted(databases) == ["default", "direct"]
    assert databases["default"]["HOST"] == "pool.example.org"
    assert databases["default"]["NAME"] == "open-pool"
    assert databases["direct"]["HOST"] == "db.example.org"
    assert databases["direct"]["PORT"] == "25060"
    assert databases["direct"]["NAME"] == "open"


def test_pooled_default_closes_connections_and_uses_no_server_cursors() -> None:
    url = f"{POOL_URL}?conn_max_age=600&sslmode=require"
    default = _databases({**POOLED, "DATABASE_URL": url})["default"]
    assert default["CONN_MAX_AGE"] == 0
    assert default["DISABLE_SERVER_SIDE_CURSORS"] is True
    # The URL keeps sslmode; settings add the default connect_timeout.
    assert default["OPTIONS"] == ["connect_timeout", "sslmode"]


def test_pooled_default_never_prepares_statements() -> None:
    default = _databases(POOLED)["default"]
    assert default["HAS_PREPARE_THRESHOLD"] is True
    assert default["PREPARE_THRESHOLD"] is None


def test_direct_closes_connections_and_mirrors_default_in_tests() -> None:
    url = f"{DIRECT_URL}?conn_max_age=600"
    direct = _databases({**POOLED, "DIRECT_DATABASE_URL": url})["direct"]
    assert direct["CONN_MAX_AGE"] == 0
    assert direct["TEST_MIRROR"] == "default"


def test_pooled_production_settings_pass_deploy_check() -> None:
    result = _check(POOLED, "--deploy", "--fail-level", "WARNING")
    assert result.returncode == 0, result.stderr


def _assert_refused(values: dict[str, str], *names: str) -> None:
    result = _check(values)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    for name in names:
        assert name in result.stderr
    assert DB_MARKER not in result.stderr
    assert DB_MARKER not in result.stdout


@pytest.mark.parametrize("option", ["prepare_threshold", "assume_role", "pool"])
def test_session_options_in_the_pool_url_are_refused(option: str) -> None:
    url = f"{POOL_URL}?sslmode=require&{option}=1"
    _assert_refused({**POOLED, "DATABASE_URL": url}, "DATABASE_URL", option)


def test_session_options_are_allowed_without_a_pool() -> None:
    # Unpooled, DATABASE_URL is the database itself, as before.
    url = "postgres://open@localhost:5432/open?prepare_threshold=5"
    result = _check({**LOCAL_DEBUG, "DATABASE_URL": url})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "url", [f"mysqlx://u:{DB_MARKER}@h/db", f"postgres://u:{DB_MARKER}@h"]
)
def test_malformed_direct_url_fails_naming_the_variable(url: str) -> None:
    _assert_refused({**POOLED, "DIRECT_DATABASE_URL": url}, "DIRECT_DATABASE_URL")


def test_malformed_pool_url_still_names_database_url() -> None:
    result = _check({**POOLED, "DATABASE_URL": f"postgres://u:{DB_MARKER}@h"})
    assert "DATABASE_URL is not a database URL" in result.stderr
    assert "DIRECT_DATABASE_URL" not in result.stderr
    assert DB_MARKER not in result.stderr

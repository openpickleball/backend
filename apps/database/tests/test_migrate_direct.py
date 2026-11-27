"""migrate works with the pool unreachable (ADR 0038).

A subprocess runs ``manage.py migrate`` with DATABASE_URL at a port where
nothing listens, standing in for a stopped PgBouncer, and
DIRECT_DATABASE_URL at a new throwaway database on the test Postgres. The
data migrations (the Moderator group) use the default alias, so they
succeed only if migrate pointed default at the direct database.
"""

import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
from django.db import connection

from config.tests.test_settings import BASE_DIR, LOCAL_DEBUG, _env

# Used when the test Postgres needs no password (trust authentication, as
# in compose.yaml and CI). It must never appear in any output.
DB_MARKER = "not-a-real-migrate-pw-MARKER"

# The pool's stand-in: nothing listens on port 1.
UNREACHABLE_POOL = "postgres://open@127.0.0.1:1/open_unreachable_pool"

MODERATOR_CODENAMES = {
    "moderate_match",
    "view_match",
    "view_matchflag",
    "verify_organizer",
    "view_organizer",
    "view_organizerreport",
    "view_organizerverificationrequest",
}


def _server_params() -> dict[str, Any]:
    params = connection.get_connection_params()
    return {
        key: params[key]
        for key in ("host", "port", "user", "password")
        if params.get(key) not in (None, "")
    }


def _admin_connect() -> psycopg.Connection[Any]:
    return psycopg.connect(**_server_params(), dbname="postgres", autocommit=True)


@pytest.fixture
def throwaway_database() -> Iterator[str]:
    name = f"open_migrate_direct_{uuid.uuid4().hex[:12]}"
    with _admin_connect() as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        with _admin_connect() as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_migrate_runs_on_the_direct_database(throwaway_database: str) -> None:
    params = _server_params()
    password = params.get("password") or DB_MARKER
    host = params.get("host", "localhost")
    port = params.get("port", 5432)
    direct_url = (
        f"postgres://{quote(params['user'], safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{throwaway_database}"
    )
    env = _env(
        {
            **LOCAL_DEBUG,
            "DATABASE_URL": UNREACHABLE_POOL,
            "DIRECT_DATABASE_URL": direct_url,
        }
    )
    result = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    assert password not in output
    assert result.returncode == 0, output

    with psycopg.connect(**params, dbname=throwaway_database) as db:
        rows = db.execute(
            "SELECT p.codename FROM auth_group g"
            " JOIN auth_group_permissions gp ON gp.group_id = g.id"
            " JOIN auth_permission p ON p.id = gp.permission_id"
            " WHERE g.name = 'Moderator'"
        ).fetchall()
    assert {codename for (codename,) in rows} >= MODERATOR_CODENAMES

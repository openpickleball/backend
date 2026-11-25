"""Database connections give up on a silent server (issue #61).

DigitalOcean's bound connection strings carry only sslmode=require. With
no connect_timeout, a connect to a Postgres that drops packets waits for
psycopg's own default of 130 seconds, so a readiness probe holds a gunicorn
worker until gunicorn kills it. Both the default alias and the pooled
direct alias get a 5 second connect_timeout, which a connect_timeout in
the URL's query still overrides.
"""

import json

from config.tests.test_settings import PRODUCTION_LIKE, _run

POOL_URL = "postgresql://open@pool.example.org:25061/open-pool?sslmode=require"
DIRECT_URL = "postgresql://open@db.example.org:25060/open?sslmode=require"

PRINT_TIMEOUTS = """
import json
import django
from django.conf import settings
from django.db import connections
django.setup()
print(json.dumps({
    alias: connections[alias].get_connection_params().get('connect_timeout')
    for alias in settings.DATABASES
}))
"""


def _timeouts(values: dict[str, str]) -> dict[str, object]:
    result = _run(["-c", PRINT_TIMEOUTS], values)
    if result.returncode != 0:
        raise AssertionError(f"settings failed to load:\n{result.stderr}")
    timeouts: dict[str, object] = json.loads(result.stdout)
    return timeouts


def test_default_gets_a_five_second_connect_timeout() -> None:
    assert _timeouts(PRODUCTION_LIKE) == {"default": 5}


def test_pooled_aliases_both_get_a_five_second_connect_timeout() -> None:
    values = {
        **PRODUCTION_LIKE,
        "DATABASE_URL": POOL_URL,
        "DIRECT_DATABASE_URL": DIRECT_URL,
    }

    assert _timeouts(values) == {"default": 5, "direct": 5}


def test_a_connect_timeout_in_the_url_wins() -> None:
    values = {
        **PRODUCTION_LIKE,
        "DATABASE_URL": f"{POOL_URL}&connect_timeout=2",
        "DIRECT_DATABASE_URL": f"{DIRECT_URL}&connect_timeout=3",
    }

    assert _timeouts(values) == {"default": 2, "direct": 3}

"""Regression tests for review findings on the Celery setup (issue #9).

The broker cases build a connection object in a fresh process with every
settings variable set, using the helpers from test_settings. Building a
connection opens no socket, so no broker is needed. Every URL and
password here is made up.
"""

import json
import re
import ssl
import subprocess
import sys
from pathlib import Path

from celery.exceptions import QueueNotFound
from django.test import SimpleTestCase
from kombu import Connection, Exchange

from config import celery_app
from config.tests.test_settings import (
    BASE_DIR,
    LOCAL_DEBUG,
    PRODUCTION_LIKE,
    _check,
    _env,
)

PRINT_CONNECTION = """
import json
import sys
import django
django.setup()
from config import celery_app
conn = celery_app.connection_for_write()
ssl_options = conn.ssl
print(json.dumps({
    'HOSTNAME': conn.hostname,
    'PORT': conn.port,
    'PASSWORD_MATCHES': conn.password == sys.argv[1],
    'ALTERNATES': len(conn.alt),
    'SSL': (
        {key: int(value) for key, value in ssl_options.items()}
        if ssl_options else None
    ),
}))
"""

ENV_OVERRIDES = (
    "CELERY_BROKER_URL",
    "CELERY_BROKER_READ_URL",
    "CELERY_BROKER_WRITE_URL",
    "CELERY_RESULT_BACKEND",
)

# Made-up host for tests only. It must never reach any output.
EVIL_HOST = "evil.example"


def _run_with(
    args: list[str], values: dict[str, str], extra: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=BASE_DIR,
        env={**_env(values), **extra},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _connection(values: dict[str, str], password: str) -> dict[str, object]:
    result = _run_with(["-c", PRINT_CONNECTION, password], values, {})
    if result.returncode != 0:
        # The error text may carry the password, so it is not shown.
        raise AssertionError("the broker connection failed to build")
    connection: dict[str, object] = json.loads(result.stdout)
    return connection


class BrokerUrlSeparatorTests(SimpleTestCase):
    """kombu splits a broker URL on ";" into failover hosts (finding A)."""

    def test_separator_in_password_stays_one_broker(self) -> None:
        url = "redis://u:pa,ss;wd@localhost:6379/0"
        conn = _connection({**LOCAL_DEBUG, "REDIS_URL": url}, "pa,ss;wd")
        assert conn["HOSTNAME"] == "localhost"
        assert conn["PORT"] == 6379
        assert conn["PASSWORD_MATCHES"] is True
        assert conn["ALTERNATES"] == 0

    def test_separator_in_password_cannot_redirect_the_broker(self) -> None:
        url = "rediss://:6380;tail@cache.example.org:6379/0"
        conn = _connection({**PRODUCTION_LIKE, "REDIS_URL": url}, "6380;tail")
        assert conn["HOSTNAME"] == "cache.example.org"
        assert conn["PORT"] == 6379
        assert conn["PASSWORD_MATCHES"] is True
        assert conn["ALTERNATES"] == 0

    def test_separator_in_host_is_refused_without_echo(self) -> None:
        result = _check({**LOCAL_DEBUG, "REDIS_URL": "redis://ho;st:6379/0"})
        assert result.returncode != 0
        assert "REDIS_URL is not a Redis URL" in result.stderr
        assert "ho;st" not in result.stderr
        assert "ho;st" not in result.stdout


class BrokerConnectionTlsTests(SimpleTestCase):
    """The connection itself carries the TLS options (finding B)."""

    def test_rediss_connection_verifies_the_certificate(self) -> None:
        conn = _connection(PRODUCTION_LIKE, "")
        assert conn["SSL"] == {"ssl_cert_reqs": int(ssl.CERT_REQUIRED)}

    def test_redis_connection_has_no_tls_options(self) -> None:
        conn = _connection(LOCAL_DEBUG, "")
        assert conn["SSL"] is None


class CeleryEnvironmentOverrideTests(SimpleTestCase):
    """Celery reads these variables ahead of settings (finding C)."""

    def test_each_override_variable_is_refused_without_echo(self) -> None:
        for name in ENV_OVERRIDES:
            with self.subTest(name=name):
                result = _run_with(
                    ["manage.py", "check"],
                    LOCAL_DEBUG,
                    {name: f"rediss://{EVIL_HOST}:6379/0"},
                )
                assert result.returncode != 0
                assert "ImproperlyConfigured" in result.stderr
                assert name in result.stderr
                assert "REDIS_URL" in result.stderr
                assert EVIL_HOST not in result.stderr
                assert EVIL_HOST not in result.stdout

    def test_empty_override_variable_is_allowed(self) -> None:
        for name in ENV_OVERRIDES:
            with self.subTest(name=name):
                result = _run_with(["manage.py", "check"], LOCAL_DEBUG, {name: ""})
                assert result.returncode == 0, result.stderr


class ComposeBeatTests(SimpleTestCase):
    """Beat waits for migrations instead of exiting (finding D)."""

    def _beat_block(self) -> str:
        compose = (Path(BASE_DIR) / "compose.yaml").read_text()
        match = re.search(r"^  beat:\n(.*?)(?=^\S|^  \w)", compose, re.M | re.S)
        assert match is not None
        return match.group(1)

    def test_beat_waits_for_migrations_then_starts(self) -> None:
        block = self._beat_block()
        check = block.find("manage.py migrate --check")
        start = block.find("exec uv run --no-sync celery -A config beat")
        assert check != -1
        assert start != -1
        assert check < start
        assert "until " in block
        assert "sleep " in block


class QueueBindingTests(SimpleTestCase):
    """Each queue binds to its own routing key (finding E)."""

    def test_each_queue_routing_key_is_its_name(self) -> None:
        for queue in celery_app.amqp.queues.values():
            assert queue.routing_key == queue.name

    def test_publish_to_default_key_lands_only_in_default(self) -> None:
        queues = list(celery_app.amqp.queues.values())
        with Connection("memory://") as conn:
            channel = conn.channel()
            bound = [queue(channel) for queue in queues]
            for queue in bound:
                queue.declare()
                queue.purge()
            conn.Producer(channel).publish(
                {"probe": True},
                exchange=Exchange("default", type="direct"),
                routing_key="default",
            )
            counts = {
                queue.name: queue.queue_declare(passive=True).message_count
                for queue in bound
            }
            for queue in bound:
                queue.purge()
        assert counts == {"default": 1, "ratings": 0, "email": 0, "webhooks": 0}


class BrokerOptionTests(SimpleTestCase):
    def test_broker_connect_times_out(self) -> None:
        # Finding F: without it a .delay() to an unreachable host hangs.
        options = celery_app.conf.broker_transport_options
        assert options["socket_connect_timeout"] == 5
        assert "socket_timeout" not in options

    def test_results_never_expire_so_beat_adds_no_cleanup_task(self) -> None:
        # Finding G: there is no result backend to clean.
        assert celery_app.conf.result_expires is None

    def test_undeclared_queue_is_refused(self) -> None:
        # Finding H: a typo must not create a queue no worker reads.
        assert celery_app.conf.task_create_missing_queues is False
        with self.assertRaises(QueueNotFound):
            celery_app.amqp.router.route({"queue": "emails"}, "config.ping")

    def test_retries_broker_connection_on_startup(self) -> None:
        # Finding I.
        assert celery_app.conf.broker_connection_retry_on_startup is True

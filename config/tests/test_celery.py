"""Tests for the Celery app, its queues, and its test setup (issue #9).

The cases are SimpleTestCase classes so that both pytest and
`manage.py test` collect them: eager mode must hold under either runner.
None of them needs a broker. The TLS cases load settings in a fresh
process, like config/tests/test_settings.py.
"""

import json
import re
import ssl
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from kombu import Connection

from config import celery_app
from config.celery import ping
from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _run

BASE_DIR = Path(__file__).resolve().parent.parent.parent

QUEUES = {"default", "ratings", "email", "webhooks"}

PRINT_BROKER = """
import json
import django
from django.conf import settings
django.setup()
from config import celery_app
ssl_options = celery_app.conf.broker_use_ssl
print(json.dumps({
    'BROKER_IS_REDIS_URL': celery_app.conf.broker_url == settings.REDIS_URL,
    'BROKER_USE_SSL': ssl_options is not None,
    'CERT_REQS': None if ssl_options is None else int(ssl_options['ssl_cert_reqs']),
}))
"""


def _queue_for(task_name: str) -> str:
    """Return the queue the app's own router picks for a task name."""
    route = celery_app.amqp.router.route({}, task_name)
    name: str = route["queue"].name
    return name


def _broker(values: dict[str, str]) -> dict[str, object]:
    result = _run(["-c", PRINT_BROKER], values)
    if result.returncode != 0:
        raise AssertionError(f"Celery app failed to load:\n{result.stderr}")
    broker: dict[str, object] = json.loads(result.stdout)
    return broker


class PingTests(SimpleTestCase):
    def test_ping_is_registered_under_its_explicit_name(self) -> None:
        assert ping.name == "config.ping"
        assert celery_app.tasks["config.ping"] is ping

    def test_ping_returns_pong(self) -> None:
        assert ping() == "pong"

    def test_delay_runs_eagerly_without_a_broker(self) -> None:
        # Any publish or broker connection fails the test, so a pass cannot
        # come from a Valkey that happens to be running.
        broker_used = AssertionError("a broker was used")
        with (
            mock.patch.object(
                celery_app.amqp, "send_task_message", side_effect=broker_used
            ),
            mock.patch.object(Connection, "connect", side_effect=broker_used),
            mock.patch.object(Connection, "ensure_connection", side_effect=broker_used),
        ):
            result = ping.delay()
        assert result.get() == "pong"


class EagerSettingsTests(SimpleTestCase):
    def test_test_settings_run_tasks_eagerly(self) -> None:
        assert settings.CELERY_TASK_ALWAYS_EAGER is True
        assert settings.CELERY_TASK_EAGER_PROPAGATES is True

    def test_app_config_runs_tasks_eagerly(self) -> None:
        assert celery_app.conf.task_always_eager is True
        assert celery_app.conf.task_eager_propagates is True


class QueueTests(SimpleTestCase):
    def test_declares_exactly_the_four_queues(self) -> None:
        names = {queue.name for queue in celery_app.conf.task_queues}
        assert names == QUEUES

    def test_default_queue_is_default(self) -> None:
        assert celery_app.conf.task_default_queue == "default"

    def test_compose_worker_consumes_every_queue(self) -> None:
        compose = (BASE_DIR / "compose.yaml").read_text()
        options = re.findall(r"-Q\s+([\w,]+)", compose)
        assert len(options) == 1
        assert set(options[0].split(",")) == QUEUES


class RoutingTests(SimpleTestCase):
    def test_rating_update_goes_to_ratings(self) -> None:
        assert _queue_for("apps.ratings.tasks.update_ratings_for_match") == "ratings"

    def test_email_task_goes_to_email(self) -> None:
        assert _queue_for("apps.emails.tasks.x") == "email"

    def test_webhook_task_goes_to_webhooks(self) -> None:
        assert _queue_for("apps.webhooks.tasks.x") == "webhooks"

    def test_ping_goes_to_default(self) -> None:
        assert _queue_for("config.ping") == "default"

    def test_unmatched_task_goes_to_default(self) -> None:
        assert _queue_for("apps.matches.tasks.x") == "default"

    def test_near_miss_goes_to_default(self) -> None:
        assert _queue_for("apps.ratingsx.tasks.x") == "default"


class DefaultsTests(SimpleTestCase):
    def test_broker_is_redis_url(self) -> None:
        # Compared, never printed: the URL may carry a password.
        assert celery_app.conf.broker_url == settings.REDIS_URL

    def test_acks_late_with_prefetch_of_one(self) -> None:
        assert celery_app.conf.task_acks_late is True
        assert celery_app.conf.worker_prefetch_multiplier == 1

    def test_lost_worker_does_not_requeue(self) -> None:
        assert not celery_app.conf.task_reject_on_worker_lost

    def test_time_limits(self) -> None:
        assert celery_app.conf.task_soft_time_limit == 300
        assert celery_app.conf.task_time_limit == 360

    def test_time_limits_stay_below_visibility_timeout(self) -> None:
        options = celery_app.conf.broker_transport_options
        assert options["visibility_timeout"] == 3600
        assert celery_app.conf.task_soft_time_limit < options["visibility_timeout"]
        assert celery_app.conf.task_time_limit < options["visibility_timeout"]

    def test_results_are_ignored(self) -> None:
        assert celery_app.conf.task_ignore_result is True

    def test_timezone_follows_django(self) -> None:
        assert celery_app.conf.timezone == settings.TIME_ZONE

    def test_beat_uses_the_database_scheduler(self) -> None:
        assert "django_celery_beat" in settings.INSTALLED_APPS
        assert (
            celery_app.conf.beat_scheduler
            == "django_celery_beat.schedulers:DatabaseScheduler"
        )


class BrokerTlsTests(SimpleTestCase):
    def test_rediss_url_verifies_the_certificate(self) -> None:
        broker = _broker(PRODUCTION_LIKE)
        assert broker["BROKER_IS_REDIS_URL"] is True
        assert broker["BROKER_USE_SSL"] is True
        assert broker["CERT_REQS"] == int(ssl.CERT_REQUIRED)

    def test_redis_url_sets_no_tls_options(self) -> None:
        broker = _broker(LOCAL_DEBUG)
        assert broker["BROKER_IS_REDIS_URL"] is True
        assert broker["BROKER_USE_SSL"] is False
        assert broker["CERT_REQS"] is None

"""Opt-in Sentry that never sends personal data (#65).

Without ``SENTRY_DSN`` the SDK never starts. With it, every error event,
transaction, and log record is scrubbed (``config/sentry.py``), and the
cron monitors check in. The in-process tests start a real Sentry client
with OPEN's options and a transport that keeps every envelope in memory,
so nothing leaves the process. Made-up data and hosts only.
"""

import io
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from typing import Any, ClassVar, cast

import pytest
import sentry_sdk
from celery import beat
from celery.schedules import crontab
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, override_settings
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event, Log

from apps.accounts.tests.headless import APP, BROWSER
from apps.ratings import services
from apps.ratings.tasks import replay_ratings
from config import sentry as open_sentry
from config.celery import app as celery_app
from config.tests.test_settings import BASE_DIR, LOCAL_DEBUG, PRODUCTION_LIKE, _env

# Made-up values that must never reach Sentry.
EMAIL = "robin.quillfeather@example.org"
TOKEN = "tok-q7z3x9-made-up"
IP = "203.0.113.77"
DSN = "https://publickey7q@sentry.example.invalid/42"
# A made-up host that must never appear in a settings error.
MARKER = "sentry-q7z.example.invalid"


# Startup, in a fresh process.

PRINT_CLIENT = """
import json
import django
import sentry_sdk
from django.conf import settings
django.setup()
client = sentry_sdk.get_client()
options = client.options
print(json.dumps({
    "active": client.is_active(),
    "dsn": settings.SENTRY_DSN,
    "send_default_pii": client.should_send_default_pii(),
    "max_request_body_size": options.get("max_request_body_size"),
    "include_local_variables": options.get("include_local_variables"),
    "environment": options.get("environment"),
    "release": options.get("release"),
    "traces_sample_rate": options.get("traces_sample_rate"),
    "enable_logs": options.get("enable_logs"),
    "hooks": [
        getattr(options.get(name), "__qualname__", None)
        for name in ("before_send", "before_send_transaction", "before_send_log")
    ],
    "integrations": sorted(client.integrations) if client.is_active() else [],
}))
"""


def _run(values: dict[str, str], **sentry: str) -> subprocess.CompletedProcess[str]:
    env = _env(values)
    env.update(sentry)
    return subprocess.run(
        [sys.executable, "-c", PRINT_CLIENT],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _loaded(values: dict[str, str], **sentry: str) -> dict[str, Any]:
    result = _run(values, **sentry)
    assert result.returncode == 0, result.stderr
    loaded: dict[str, Any] = json.loads(result.stdout)
    return loaded


@pytest.mark.parametrize("values", [PRODUCTION_LIKE, LOCAL_DEBUG])
@pytest.mark.parametrize("dsn", ["", "   "])
def test_without_a_dsn_sentry_never_starts(values: dict[str, str], dsn: str) -> None:
    loaded = _loaded(values, SENTRY_DSN=dsn, SENTRY_ENABLE_LOGS="true")

    assert loaded["active"] is False
    assert loaded["dsn"] == ""


def test_the_test_run_has_no_client() -> None:
    assert settings.SENTRY_DSN == ""
    assert sentry_sdk.get_client().is_active() is False


def test_a_dsn_starts_sentry_with_the_privacy_options() -> None:
    loaded = _loaded(PRODUCTION_LIKE, SENTRY_DSN=DSN, SENTRY_RELEASE="v0.1.0")

    assert loaded["active"] is True
    assert loaded["send_default_pii"] is False
    assert loaded["max_request_body_size"] == "never"
    assert loaded["include_local_variables"] is False
    assert loaded["environment"] == "production"
    assert loaded["release"] == "v0.1.0"
    assert loaded["traces_sample_rate"] == 0.05
    assert loaded["enable_logs"] is False
    assert loaded["hooks"] == [
        "before_send",
        "before_send_transaction",
        "before_send_log",
    ]
    assert {"django", "celery"} <= set(loaded["integrations"])


def test_values_are_read_from_the_environment() -> None:
    loaded = _loaded(
        LOCAL_DEBUG,
        SENTRY_DSN="http://publickey7q@localhost:9000/3",
        SENTRY_TRACES_SAMPLE_RATE="1",
        SENTRY_ENABLE_LOGS="on",
        SENTRY_ENVIRONMENT="local-dev",
    )

    assert loaded["active"] is True
    assert loaded["environment"] == "local-dev"
    assert loaded["traces_sample_rate"] == 1.0
    assert loaded["enable_logs"] is True


def test_environment_defaults_to_development_with_debug_on() -> None:
    loaded = _loaded(LOCAL_DEBUG, SENTRY_DSN=DSN)

    assert loaded["environment"] == "development"


@pytest.mark.parametrize(
    ("name", "value", "values"),
    [
        ("SENTRY_DSN", f"not a dsn {MARKER}", PRODUCTION_LIKE),
        ("SENTRY_DSN", f"ftp://publickey@{MARKER}/42", PRODUCTION_LIKE),
        ("SENTRY_DSN", f"http://publickey@{MARKER}/42", PRODUCTION_LIKE),
        ("SENTRY_DSN", f"https://{MARKER}/42", PRODUCTION_LIKE),
        ("SENTRY_DSN", f"https://publickey:secret@{MARKER}/42", PRODUCTION_LIKE),
        ("SENTRY_DSN", f"https://publickey@{MARKER}/project", PRODUCTION_LIKE),
        ("SENTRY_DSN", f"https://publickey@{MARKER}/42?x=1", PRODUCTION_LIKE),
        ("SENTRY_DSN", f"https://publickey@{MARKER}:99999/42", PRODUCTION_LIKE),
        ("SENTRY_ENVIRONMENT", f"has space {MARKER}", LOCAL_DEBUG),
        ("SENTRY_ENVIRONMENT", f"a/{MARKER}", LOCAL_DEBUG),
        ("SENTRY_RELEASE", f"v1/{MARKER}", LOCAL_DEBUG),
        ("SENTRY_TRACES_SAMPLE_RATE", f"1.5{MARKER}", LOCAL_DEBUG),
        ("SENTRY_TRACES_SAMPLE_RATE", "1.5", LOCAL_DEBUG),
        ("SENTRY_TRACES_SAMPLE_RATE", "-0.1", LOCAL_DEBUG),
        ("SENTRY_ENABLE_LOGS", f"maybe{MARKER}", LOCAL_DEBUG),
    ],
)
def test_a_bad_value_stops_startup_without_echoing_it(
    name: str, value: str, values: dict[str, str]
) -> None:
    result = _run(values, **{name: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    assert MARKER not in result.stdout + result.stderr


# The scrub hooks, on sample events.


def _dumped(value: Any) -> str:
    return json.dumps(value, default=str)


def _assert_clean(text: str) -> None:
    for secret in (EMAIL, TOKEN, IP):
        at = text.find(secret)
        assert at == -1, text[max(at - 300, 0) : at + 60]


def test_scrub_text_replaces_emails_tokens_secrets_and_ips() -> None:
    text = (
        f"login {EMAIL} from {IP} with Bearer {TOKEN} and "
        f"?client_secret={TOKEN}&password={TOKEN}"
    )

    scrubbed = open_sentry.scrub_text(text)

    _assert_clean(scrubbed)
    assert scrubbed.startswith("login [Filtered] from [Filtered] with Bearer")


def test_before_send_scrubs_an_error_event() -> None:
    event: dict[str, Any] = {
        "level": "error",
        "message": f"Failed for {EMAIL}",
        "logentry": {"message": "Failed for %s", "params": [EMAIL]},
        "exception": {
            "values": [{"type": "ValueError", "value": f"bad token={TOKEN}"}]
        },
        "user": {"id": "1", "email": EMAIL, "ip_address": IP},
        "request": {
            "method": "POST",
            "url": f"https://api.example.org/oauth/token/?code={TOKEN}",
            "query_string": f"email={EMAIL}",
            "data": {"password": TOKEN, "email": EMAIL},
            "cookies": {"sessionid": TOKEN},
            "headers": {
                "Host": "api.example.org",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}",
                "Cookie": f"sessionid={TOKEN}",
                "X-Session-Token": TOKEN,
                "X-Email-Verification-Key": TOKEN,
                "X-Password-Reset-Key": TOKEN,
                "X-Device-Token": TOKEN,
                "Do-Connecting-Ip": IP,
                "X-Forwarded-For": IP,
                "User-Agent": f"made-up-agent {TOKEN}",
                "Referer": f"https://app.example.org/?email={EMAIL}",
            },
            "env": {"REMOTE_ADDR": IP, "SERVER_NAME": "web", "SERVER_PORT": "8000"},
        },
        "extra": {"client_secret": TOKEN, "note": f"for {EMAIL}"},
        "contexts": {"celery-job": {"args": [EMAIL], "kwargs": {"to": EMAIL}}},
        "tags": {"session_key": TOKEN},
        "breadcrumbs": {
            "values": [
                {"category": "redis", "message": f"GET :1:throttle:{IP}", "data": {}},
                {
                    "category": "httplib",
                    "data": {
                        "url": f"https://api.example.org/range/{TOKEN}",
                        "http.query": f"key={TOKEN}",
                        "method": "GET",
                    },
                },
                {"category": "django.request", "message": f"Bad request {EMAIL}"},
            ]
        },
        "modules": {"django-oauth-toolkit": "3.4.1"},
    }

    scrubbed = cast(dict[str, Any], open_sentry.before_send(cast(Event, event), {}))

    _assert_clean(_dumped(scrubbed))
    assert "user" not in scrubbed
    assert scrubbed["request"] == {
        "method": "POST",
        "url": "https://api.example.org/oauth/token/",
        "headers": {"Host": "api.example.org", "Content-Type": "application/json"},
        "env": {"SERVER_NAME": "web", "SERVER_PORT": "8000"},
    }
    crumbs = scrubbed["breadcrumbs"]["values"]
    assert crumbs[0]["message"] == "GET [Filtered]"
    assert crumbs[1]["data"] == {"url": "https://api.example.org", "method": "GET"}
    assert scrubbed["modules"] == {"django-oauth-toolkit": "3.4.1"}
    assert scrubbed["exception"]["values"][0]["type"] == "ValueError"


def test_before_send_transaction_scrubs_spans() -> None:
    event: dict[str, Any] = {
        "type": "transaction",
        "transaction": "/api/v1/players/{code}",
        "request": {"url": f"https://api.example.org/x?email={EMAIL}"},
        "spans": [
            {
                "op": "db.redis",
                "description": f"GET ':1:rl:login:{EMAIL}'",
                "data": {
                    "db.redis.key": EMAIL,
                    "redis.key": EMAIL,
                    "db.system": "redis",
                },
            },
            {
                "op": "cache.get",
                "description": f":1:session:{TOKEN}",
                "data": {"cache.key": [TOKEN], "cache.hit": True},
            },
            {
                "op": "http.client",
                "description": f"GET https://api.example.org/range/{TOKEN}?x={TOKEN}",
                "data": {
                    "url": f"https://api.example.org/range/{TOKEN}",
                    "http.query": f"x={TOKEN}",
                    "http.method": "GET",
                },
            },
            {
                "op": "db",
                "description": 'SELECT 1 FROM "accounts_user" WHERE "email" = %s',
                "data": {"db.system": "postgresql"},
            },
        ],
    }

    scrubbed = cast(
        dict[str, Any], open_sentry.before_send_transaction(cast(Event, event), {})
    )

    _assert_clean(_dumped(scrubbed))
    redis, cache, http, sql = scrubbed["spans"]
    assert redis["description"] == "GET [Filtered]"
    assert redis["data"] == {"db.system": "redis"}
    assert cache["description"] == "cache.get"
    assert cache["data"] == {}
    assert http["description"] == "GET https://api.example.org"
    assert http["data"] == {"url": "https://api.example.org", "http.method": "GET"}
    assert sql["description"] == 'SELECT 1 FROM "accounts_user" WHERE "email" = %s'


def test_before_send_log_scrubs_the_record() -> None:
    log: dict[str, Any] = {
        "severity_text": "warning",
        "body": f"Sign-in for {EMAIL} from {IP}",
        "attributes": {
            "sentry.message.template": "Sign-in for %s from %s",
            "sentry.message.parameter.0": EMAIL,
            "sentry.message.parameter.1": IP,
            "session_key": TOKEN,
            "logger.name": "apps.accounts",
        },
    }

    scrubbed = cast(dict[str, Any], open_sentry.before_send_log(cast(Log, log), {}))

    _assert_clean(_dumped(scrubbed))
    assert scrubbed["attributes"]["logger.name"] == "apps.accounts"
    assert scrubbed["attributes"]["session_key"] == "[Filtered]"


# A real client, with a transport that keeps envelopes in memory.


class _Captured(Transport):
    envelopes: ClassVar[list[Envelope]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)


def _items(kind: str) -> list[dict[str, Any]]:
    items = []
    for envelope in _Captured.envelopes:
        for item in envelope.items:
            if item.type == kind and item.payload.json is not None:
                items.append(item.payload.json)
    return items


def _everything_sent() -> str:
    return "\n".join(
        envelope.serialize().decode("utf-8", "replace")
        for envelope in _Captured.envelopes
    )


@pytest.fixture
def sentry_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start Sentry with OPEN's init_sentry() and an in-memory transport."""
    real_init = sentry_sdk.init
    _Captured.envelopes = []

    def init(**options: Any) -> None:
        real_init(**options, transport=_Captured)

    monkeypatch.setattr(sentry_sdk, "init", init)
    open_sentry.init_sentry(
        dsn=DSN,
        environment="test",
        release=None,
        traces_sample_rate=1.0,
        enable_logs=True,
    )
    yield
    client = sentry_sdk.get_client()
    client.flush()
    client.close()
    sentry_sdk.get_global_scope().set_client(None)


DEEP_LIST = "[" * 100_000


@pytest.mark.django_db
@pytest.mark.parametrize("base", [APP, BROWSER])
def test_malformed_allauth_json_sends_no_error_event(
    sentry_on: None, base: str
) -> None:
    response = Client(raise_request_exception=False).post(
        f"{base}/auth/login", DEEP_LIST, content_type="application/json"
    )
    sentry_sdk.flush()

    assert response.status_code == 400
    assert _items("event") == []


@pytest.mark.django_db
def test_without_the_middleware_the_same_body_is_an_error_event(
    sentry_on: None,
) -> None:
    # The control for the test above: the error reaches Sentry, scrubbed,
    # when HeadlessJsonErrorMiddleware (#337) does not answer it.
    middleware = [
        name
        for name in settings.MIDDLEWARE
        if name != "apps.accounts.middleware.HeadlessJsonErrorMiddleware"
    ]
    with override_settings(MIDDLEWARE=middleware):
        response = Client(raise_request_exception=False).post(
            f"{APP}/auth/login?email={EMAIL}",
            DEEP_LIST,
            content_type="application/json",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Cookie": f"sessionid={TOKEN}",
            },
        )
    sentry_sdk.flush()

    assert response.status_code == 500
    events = _items("event")
    assert [e["exception"]["values"][-1]["type"] for e in events] == ["RecursionError"]
    assert "data" not in events[0]["request"]
    _assert_clean(_everything_sent())


@pytest.mark.django_db
def test_sql_spans_and_breadcrumbs_carry_no_parameters(sentry_on: None) -> None:
    with sentry_sdk.start_transaction(name="made-up", op="test"):
        assert not get_user_model().objects.filter(email=EMAIL).exists()
        sentry_sdk.capture_message("after the query")
    sentry_sdk.flush()

    sent = _everything_sent()
    assert "accounts_user" in sent
    _assert_clean(sent)


def test_log_records_and_error_logs_are_scrubbed(sentry_on: None) -> None:
    log = logging.getLogger("apps.made_up")
    log.warning("Sign-in for %s from %s", EMAIL, IP)
    log.error("Could not send to %s with token=%s", EMAIL, TOKEN)
    sentry_sdk.get_client().flush()

    assert _items("event"), "the error log is an event"
    sent = _everything_sent()
    assert "Sign-in for" in sent
    _assert_clean(sent)


def test_disallowed_host_errors_are_not_sent(sentry_on: None) -> None:
    response = Client(raise_request_exception=False).get(
        "/api/v1/accounts/me", headers={"Host": MARKER}
    )
    sentry_sdk.flush()

    assert response.status_code == 400
    assert _items("event") == []


# Cron monitors.


def _check_ins() -> list[dict[str, Any]]:
    return _items("check_in")


def test_replay_monitor_matches_the_beat_entry() -> None:
    minute, hour, day, month, weekday = str(
        open_sentry.REPLAY_MONITOR_CONFIG["schedule"]["value"]
    ).split()
    entry = settings.CELERY_BEAT_SCHEDULE[open_sentry.REPLAY_MONITOR_SLUG]

    assert entry["task"] == "apps.ratings.tasks.replay_ratings"
    assert entry["schedule"] == crontab(
        minute=minute,
        hour=hour,
        day_of_month=day,
        month_of_year=month,
        day_of_week=weekday,
    )
    assert open_sentry.REPLAY_MONITOR_CONFIG["timezone"] == settings.CELERY_TIMEZONE
    # The task's hard time limit, in minutes.
    assert (
        replay_ratings.time_limit
        == open_sentry.REPLAY_MONITOR_CONFIG["max_runtime"] * 60
    )


def _apply_beat_entry(name: str) -> dict[str, Any]:
    entry_settings: dict[str, Any] = settings.CELERY_BEAT_SCHEDULE[name]
    sent: list[dict[str, Any]] = []

    class _Scheduler(beat.Scheduler):
        def apply_async(
            self,
            entry: beat.ScheduleEntry,
            producer: Any = None,
            advance: bool = True,
            **kwargs: Any,
        ) -> None:
            sent.append(dict(entry.options or {}))

    scheduler = _Scheduler(app=celery_app, lazy=True)
    entry = beat.ScheduleEntry(
        name=name,
        task=entry_settings["task"],
        schedule=entry_settings["schedule"],
        options=dict(entry_settings.get("options", {})),
        app=celery_app,
    )
    scheduler.apply_entry(entry)
    return sent[0]


def test_beat_entries_become_cron_monitors(sentry_on: None) -> None:
    options = _apply_beat_entry("expire-unconfirmed-matches")
    sentry_sdk.flush()

    assert options["headers"]["sentry-monitor-slug"] == "expire-unconfirmed-matches"
    [check_in] = _check_ins()
    assert check_in["monitor_slug"] == "expire-unconfirmed-matches"
    assert check_in["status"] == "in_progress"
    assert check_in["monitor_config"]["schedule"] == {
        "type": "crontab",
        "value": "0 3 * * *",
    }


def test_the_replay_entry_is_left_to_the_task(sentry_on: None) -> None:
    options = _apply_beat_entry(open_sentry.REPLAY_MONITOR_SLUG)
    sentry_sdk.flush()

    assert "sentry-monitor-slug" not in options.get("headers", {})
    assert _check_ins() == []


@pytest.mark.django_db
def test_the_replay_task_checks_in_with_a_maximum_runtime(sentry_on: None) -> None:
    replay_ratings.apply().get()
    sentry_sdk.flush()

    check_ins = _check_ins()
    assert [c["status"] for c in check_ins] == ["in_progress", "ok"]
    assert {c["monitor_slug"] for c in check_ins} == {"replay-ratings"}
    assert check_ins[0]["monitor_config"]["max_runtime"] == 30


@pytest.mark.django_db
def test_a_failed_replay_checks_in_as_an_error(
    sentry_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> None:
        raise RuntimeError(f"made-up failure for {EMAIL}")

    monkeypatch.setattr(services, "replay_ratings", fail)

    with pytest.raises(RuntimeError):
        replay_ratings.apply().get()
    sentry_sdk.flush()

    assert [c["status"] for c in _check_ins()] == ["in_progress", "error"]
    _assert_clean(_everything_sent())


@pytest.mark.django_db
def test_recompute_ratings_checks_in(sentry_on: None) -> None:
    call_command("recompute_ratings", "--dry-run", stdout=io.StringIO())
    sentry_sdk.flush()

    check_ins = _check_ins()
    assert [c["status"] for c in check_ins] == ["in_progress", "ok"]
    assert {c["monitor_slug"] for c in check_ins} == {"recompute-ratings"}
    assert check_ins[0]["monitor_config"]["max_runtime"] == 60

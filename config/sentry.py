"""Opt-in error, trace, log, and cron monitoring with Sentry (#65).

``config/settings.py`` calls ``init_sentry()`` only when ``SENTRY_DSN`` is
set, so local development, tests, and self-hosted deployments without it
send nothing. Nothing here reads the environment.

Sentry must never receive personal data (hard rule 1, ADR 0009). The SDK
runs without its default personal data (``send_default_pii`` off), no
request bodies, and no local
variables in stack frames, and every event, transaction, and log record
passes through the scrub hooks below before it leaves the process:

- request bodies, cookies, query strings, and every header and WSGI
  value outside a short allowlist are dropped, and the URL loses its
  query string;
- the user is dropped;
- a value under a key that names a secret or personal data (a password,
  token, secret, cookie, session, CSRF value, authorization, signature,
  email, IP address, or any key ending in ``key``) becomes
  ``[Filtered]``;
- in every remaining string, email addresses, bearer tokens,
  ``name=value`` secrets, and IPv4 addresses are replaced;
- Valkey spans and breadcrumbs keep only the command name, never the
  key, and outgoing HTTP spans and breadcrumbs keep only the method,
  scheme, and host.

SQL spans and breadcrumbs carry the query text with placeholders only:
the SDK sends parameters only with ``send_default_pii``, which stays off.
"""

import re
from collections.abc import Mapping
from typing import Any, Final, cast
from urllib.parse import urlsplit

import sentry_sdk
from django.http import HttpRequest
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import ignore_logger, ignore_logger_for_sentry_logs
from sentry_sdk.scrubber import EventScrubber
from sentry_sdk.types import Event, Hint, Log, MonitorConfig

FILTERED: Final = "[Filtered]"

# Cron monitors (#65). Every Celery Beat entry becomes a monitor named
# after the entry, with the entry's schedule, through the Celery
# integration: beat checks in when it sends the task and the worker when
# the task ends, so a job that stops running, never reaches a worker, or
# fails shows as a failed monitor. The replay entry is left out of the
# automatic monitors and checks in from the task itself, so its monitor
# can carry a maximum runtime. Only the runs beat sent check in: the task
# is also queued after match changes, and those runs must not hide a beat
# that stopped.
REPLAY_MONITOR_SLUG: Final = "replay-ratings"
# The beat entry's schedule (CELERY_BEAT_SCHEDULE["replay-ratings"], a
# test keeps them equal). A run missed for 15 minutes is dropped, so the
# margin is one interval. The task's hard time limit is 30 minutes.
REPLAY_MONITOR_CONFIG: Final[MonitorConfig] = {
    "schedule": {"type": "crontab", "value": "*/15 * * * *"},
    "timezone": "UTC",
    "checkin_margin": 15,
    "max_runtime": 30,
}
# recompute_ratings (#48, ADR 0058) is run by hand, after an engine or
# spec change, so it has no real schedule. Sentry needs one, so it is a
# year, with a day's margin: the monitor reports a run that fails or
# passes its maximum runtime, and a year with no run at all as missed.
RECOMPUTE_MONITOR_SLUG: Final = "recompute-ratings"
RECOMPUTE_MONITOR_CONFIG: Final[MonitorConfig] = {
    "schedule": {"type": "interval", "value": 1, "unit": "year"},
    "timezone": "UTC",
    "checkin_margin": 1440,
    "max_runtime": 60,
}

# Request headers and WSGI values kept on an event. Everything else goes:
# cookies, Authorization, allauth's session token and emailed keys, the
# device token, the client IP headers, the user agent, and the referer
# (which can carry a query string).
_KEPT_HEADERS: Final = frozenset(
    {"accept", "accept-encoding", "content-length", "content-type", "host", "origin"}
)
_KEPT_ENV: Final = frozenset({"SERVER_NAME", "SERVER_PORT"})
# Log record attributes kept: the SDK's own, the logger, the code
# location, and the server.
_KEPT_LOG_ATTRIBUTE_PREFIXES: Final = (
    "sentry.",
    "logger.",
    "code.",
    "server.",
    "thread.",
    "process.",
)

# Keys whose value is always dropped, compared lowercased. A key that
# contains one of these words, or ends in "key", is filtered.
_SENSITIVE_KEY_WORDS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "session",
    "csrf",
    "signature",
    "email",
    "ip_address",
    "remote_addr",
    "forwarded",
    "client-ip",
    "client_ip",
    "connecting-ip",
    "real-ip",
    "dsn",
    "user_agent",
    "user-agent",
)
# Sentry's own keys that look sensitive but are not: the DSN's public key
# in the trace's dynamic sampling context, which Sentry needs.
_KEPT_KEYS: Final = frozenset({"public_key"})
_SENSITIVE_KEYS: Final = frozenset({"code", "keys", "args", "kwargs", "user"})

_EMAIL: Final = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}"
    r"(?:\.[A-Za-z0-9-]{1,63}){1,8}"
)
_BEARER: Final = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]+")
_NAMED_SECRET: Final = re.compile(
    r"(?i)\b([\w.-]{0,64}(?:password|passwd|secret|token|key|code|session|csrf|"
    r"signature|email|dsn)[\w.-]{0,64})=([^&\s;,'\"]+)"
)
_COLON_SECRET: Final = re.compile(
    r"(?i)\b([\w.-]{0,64}(?:password|passwd|secret|token)[\w.-]{0,64}"
    r"(?<!error)(?<!exception)(?<!warning)[\"']?:\s*)('[^']*'|\"[^\"]*\"|\S+)"
)
# Postgres adds the failing row or key values after DETAIL.
_DETAIL: Final = re.compile(r"DETAIL:.*", re.DOTALL)
_IPV4: Final = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Candidate IPv6 addresses: hex groups and colons. Only a run with "::"
# or at least five colons counts, so a time such as 12:30:45 is kept.
_IPV6: Final = re.compile(
    r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])"
)
# Values pydantic and Postgres quote in their error messages.
# pydantic's input_value runs to its ", input_type=" or the line's end,
# so a dict or list value goes whole.
_INPUT_VALUE: Final = re.compile(r"input_value=.*?(?=, input_type=|$)", re.MULTILINE)
_KEY_DETAIL: Final = re.compile(r"(Key \([^)]*\))=\([^)]*\)")


# Longer text is cut first, so scrubbing stays cheap on any input.
_MAX_TEXT: Final = 16384


def scrub_text(text: str) -> str:
    """Replace emails, tokens, named secrets, and IP addresses in text."""
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + " [truncated]"
    text = _IPV4.sub(FILTERED, text)
    text = _EMAIL.sub(FILTERED, text)
    text = _BEARER.sub(lambda match: f"{match.group(1)} {FILTERED}", text)
    text = _NAMED_SECRET.sub(lambda match: f"{match.group(1)}={FILTERED}", text)
    text = _COLON_SECRET.sub(lambda match: f"{match.group(1)}{FILTERED}", text)
    text = _DETAIL.sub(f"DETAIL: {FILTERED}", text)
    text = _INPUT_VALUE.sub(f"input_value={FILTERED}", text)
    text = _KEY_DETAIL.sub(lambda match: f"{match.group(1)}=({FILTERED})", text)
    return _IPV6.sub(_ipv6, text)


def _ipv6(match: re.Match[str]) -> str:
    found = match.group(0)
    return FILTERED if "::" in found or found.count(":") >= 5 else found


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _KEPT_KEYS:
        return False
    return (
        lowered in _SENSITIVE_KEYS
        or lowered.endswith("key")
        or any(word in lowered for word in _SENSITIVE_KEY_WORDS)
    )


def _scrub(value: Any) -> Any:
    """Scrub a value recursively: filter sensitive keys, clean every string."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {
            key: (
                FILTERED
                if isinstance(key, str) and _is_sensitive_key(key)
                else _scrub(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub(item) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    # Any other object is sent as its type only. Its text can hold
    # anything: a request's repr, for example, holds its query string.
    if isinstance(value, HttpRequest):
        return f"<{type(value).__name__}: {value.method}>"
    return f"<{type(value).__name__}>"


def _without_query(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return FILTERED
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else url


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return FILTERED
    return f"{parts.scheme}://{host}{port}" if parts.scheme else FILTERED


def _scrub_request(request: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    if isinstance(request.get("method"), str):
        kept["method"] = request["method"]
    if isinstance(request.get("url"), str):
        kept["url"] = _without_query(request["url"])
    headers = request.get("headers")
    if isinstance(headers, Mapping):
        kept["headers"] = {
            name: value
            for name, value in headers.items()
            if str(name).lower() in _KEPT_HEADERS
        }
    env = request.get("env")
    if isinstance(env, Mapping):
        kept["env"] = {name: value for name, value in env.items() if name in _KEPT_ENV}
    return cast(dict[str, Any], _scrub(kept))


def _command_only(description: str) -> str:
    command = description.split(" ", 1)[0]
    return f"{command} {FILTERED}" if command != description else command


def _scrub_span(span: dict[str, Any]) -> dict[str, Any]:
    op = str(span.get("op") or "")
    description = span.get("description")
    data = span.get("data")
    if op.startswith(("db.redis", "cache")):
        if op.startswith("cache"):
            # A cache span's description is the key itself.
            span["description"] = op
        elif isinstance(description, str):
            span["description"] = _command_only(description)
        if isinstance(data, dict):
            span["data"] = {
                key: value
                for key, value in data.items()
                if not str(key).startswith(("cache.", "db.redis.", "redis."))
            }
    elif op.startswith("template"):
        if isinstance(data, dict):
            # A rendered template's whole context.
            data.pop("context", None)
    elif op.startswith("http.client"):
        if isinstance(description, str):
            method, _, url = description.partition(" ")
            span["description"] = f"{method} {_origin(url)}" if url else method
        if isinstance(data, dict):
            span["data"] = {
                key: (
                    _origin(value) if key == "url" and isinstance(value, str) else value
                )
                for key, value in data.items()
                if key not in {"http.query", "http.fragment"}
            }
    return cast(dict[str, Any], _scrub(span))


def _scrub_breadcrumb(crumb: dict[str, Any]) -> dict[str, Any]:
    category = str(crumb.get("category") or "")
    if category == "redis" and isinstance(crumb.get("message"), str):
        crumb["message"] = _command_only(crumb["message"])
        crumb["data"] = {}
    elif category in {"httplib", "http"} and isinstance(crumb.get("data"), dict):
        data = crumb["data"]
        crumb["data"] = {
            key: (_origin(value) if key == "url" and isinstance(value, str) else value)
            for key, value in data.items()
            if key not in {"http.query", "http.fragment"}
        }
    return cast(dict[str, Any], _scrub(crumb))


def scrub_event(event: Event) -> Event:
    """Remove personal data and secrets from an error event or transaction."""
    raw = cast(dict[str, Any], event)
    raw.pop("user", None)
    logentry = raw.get("logentry")
    if isinstance(logentry, dict):
        # The formatted message is kept and scrubbed. A parameter alone
        # cannot be told apart from a secret, so the parameters go.
        logentry.pop("params", None)
    request = raw.pop("request", None)
    breadcrumbs = raw.pop("breadcrumbs", None)
    spans = raw.pop("spans", None)
    # Package names and versions only. Skipped, so a package such as
    # django-oauth-toolkit keeps its version.
    modules = raw.pop("modules", None)
    scrubbed = cast(dict[str, Any], _scrub(raw))
    if isinstance(request, dict):
        scrubbed["request"] = _scrub_request(request)
    if isinstance(breadcrumbs, dict) and isinstance(breadcrumbs.get("values"), list):
        scrubbed["breadcrumbs"] = {
            **breadcrumbs,
            "values": [
                _scrub_breadcrumb(crumb)
                for crumb in breadcrumbs["values"]
                if isinstance(crumb, dict)
            ],
        }
    elif isinstance(breadcrumbs, list):
        scrubbed["breadcrumbs"] = [
            _scrub_breadcrumb(crumb) for crumb in breadcrumbs if isinstance(crumb, dict)
        ]
    if isinstance(spans, list):
        scrubbed["spans"] = [
            _scrub_span(span) for span in spans if isinstance(span, dict)
        ]
    if modules is not None:
        scrubbed["modules"] = modules
    return cast(Event, scrubbed)


def before_send(event: Event, hint: Hint) -> Event | None:
    """Sentry's ``before_send``: scrub every error event."""
    return scrub_event(event)


def before_send_transaction(event: Event, hint: Hint) -> Event | None:
    """Sentry's ``before_send_transaction``: scrub every transaction."""
    return scrub_event(event)


def before_send_log(log: Log, hint: Hint) -> Log | None:
    """Sentry's ``before_send_log``: scrub every log record's text and attributes."""
    raw = cast(dict[str, Any], log)
    if isinstance(raw.get("body"), str):
        raw["body"] = scrub_text(raw["body"])
    attributes = raw.get("attributes")
    if isinstance(attributes, Mapping):
        # Only the SDK's own attributes and the logger's place in the code
        # are kept. A record's extras (Django passes the request) and the
        # request's attributes (the user agent, the URL) all go. The body
        # is the formatted message, so the separate parameters go too: a
        # parameter alone cannot be told apart from a secret.
        raw["attributes"] = {
            key: _scrub(value)
            for key, value in attributes.items()
            if str(key).startswith(_KEPT_LOG_ATTRIBUTE_PREFIXES)
            and not str(key).startswith("sentry.message.parameter")
        }
    return cast(Log, raw)


def init_sentry(
    *,
    dsn: str,
    environment: str,
    release: str | None,
    traces_sample_rate: float,
    enable_logs: bool,
) -> None:
    """Start the Sentry SDK. Settings call this only when ``SENTRY_DSN`` is set."""
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        traces_sample_rate=traces_sample_rate,
        enable_logs=enable_logs,
        # send_default_pii stays unset, which is off: the SDK sends no user,
        # IP address, cookies, SQL parameters, or Valkey values. sentry-sdk
        # 2.77 warns that passing it is deprecated; a test checks that it
        # is still off.
        max_request_body_size="never",
        include_local_variables=False,
        event_scrubber=EventScrubber(recursive=True),
        before_send=before_send,
        before_send_transaction=before_send_transaction,
        before_send_log=before_send_log,
        integrations=[
            # URL patterns name transactions, never the path with its codes.
            # Cache spans stay off: cache keys can hold a client IP.
            DjangoIntegration(transaction_style="url", cache_spans=False),
            CeleryIntegration(
                monitor_beat_tasks=True,
                exclude_beat_tasks=[f"^{re.escape(REPLAY_MONITOR_SLUG)}$"],
            ),
        ],
    )
    # Django logs every request for a host outside ALLOWED_HOSTS at error
    # level. Scanners send these all day; they are not OPEN's errors.
    ignore_logger("django.security.DisallowedHost")
    ignore_logger_for_sentry_logs("django.security.DisallowedHost")

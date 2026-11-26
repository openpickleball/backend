"""The breached password check fails open on every outage (#100 review).

Found in review: an HTTP protocol error (a body cut mid-read) escaped the
fail-open catch, a lone surrogate in a password raised before the check,
the transport followed redirects, had no total deadline or body cap, a
repeated suffix line took the last count, and an empty answer counted as
"not listed". Each case must accept the password with the fixed warning,
or, for a listed password, refuse it. A local stub server stands in for
the service; nothing reaches the network. Made-up data only.
"""

import logging
import threading
import time
from collections.abc import Iterator
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest
from django.test import Client

from apps.accounts import breached
from apps.accounts.tests.breach import PADDING_LINE, FakeBreachService, sha1_hex

pytestmark = pytest.mark.django_db

PASSWORD = "made-up-breached-Passw0rd"
WARNING = "The breached password check is unavailable"


class _Stub(BaseHTTPRequestHandler):
    """Answers by path: /slow/, /big/, /redirect/, /target/."""

    hits: ClassVar[list[str]] = []

    def log_message(self, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        _Stub.hits.append(self.path)
        if self.path.startswith("/slow/"):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(20):
                self.wfile.write(b"0")
                self.wfile.flush()
                time.sleep(0.3)
        elif self.path.startswith("/big/"):
            line = f"{'0' * 35}:0\r\n".encode()
            body = line * (2 * 1024 * 1024 // len(line) + 1)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", "/target/" + self.path.rsplit("/", 1)[-1])
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            digest = sha1_hex(PASSWORD)
            body = f"{digest[5:]}:9\r\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


@pytest.fixture
def stub() -> Iterator[str]:
    _Stub.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def real_transport(
    breach_service: FakeBreachService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(breached, "fetch_range", breach_service.original)


def _fails_open(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="apps.accounts.breached"):
        assert not breached.is_breached(PASSWORD)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert WARNING in messages[0]


def test_a_body_cut_mid_read_fails_open(
    breach_service: FakeBreachService, caplog: pytest.LogCaptureFixture
) -> None:
    breach_service.add(PASSWORD)
    breach_service.error = IncompleteRead(b"")

    _fails_open(caplog)


def test_a_lone_surrogate_does_not_crash_the_password_check() -> None:
    body = '{"password": "\\ud800abcdefgh-XYZ-123"}'

    response = Client().post(
        "/_allauth/app/v1/auth/signup/password-check",
        body,
        content_type="application/json",
    )

    assert response.status_code in (200, 400)


def test_a_repeated_suffix_takes_its_highest_count(
    breach_service: FakeBreachService,
) -> None:
    suffix = sha1_hex(PASSWORD)[5:]
    breach_service.body = f"{suffix}:5\r\n{PADDING_LINE}\r\n{suffix}:0"

    assert breached.is_breached(PASSWORD)


def test_an_empty_answer_fails_open(
    breach_service: FakeBreachService, caplog: pytest.LogCaptureFixture
) -> None:
    breach_service.body = ""

    _fails_open(caplog)


@pytest.mark.usefixtures("real_transport")
def test_a_redirect_is_not_followed(
    stub: str, settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings.PASSWORD_BREACH_API_URL = f"{stub}/redirect/"

    _fails_open(caplog)

    assert not any(hit.startswith("/target/") for hit in _Stub.hits)


@pytest.mark.usefixtures("real_transport")
def test_a_slow_body_stops_at_the_total_deadline(
    stub: str, settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings.PASSWORD_BREACH_API_URL = f"{stub}/slow/"
    settings.PASSWORD_BREACH_TIMEOUT_SECONDS = 1
    started = time.monotonic()

    _fails_open(caplog)

    assert time.monotonic() - started < 2.5


@pytest.mark.usefixtures("real_transport")
def test_an_oversized_body_is_refused(
    stub: str, settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings.PASSWORD_BREACH_API_URL = f"{stub}/big/"

    _fails_open(caplog)


def test_the_body_cap_is_documented() -> None:
    assert breached.MAX_BODY_BYTES == 1024 * 1024

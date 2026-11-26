"""A body the server cuts short fails open through the real transport
(#100 review, found in the live drive).

``read1`` returns an empty chunk at the end of the connection without
raising, so a body shorter than its Content-Length looked complete: a
cut answer that happened to parse was counted as "not listed", with no
warning. It must fail open with the warning. A local stub server stands
in for the service. Made-up data only.
"""

import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from apps.accounts import breached
from apps.accounts.tests.breach import PADDING_LINE, FakeBreachService

pytestmark = pytest.mark.django_db


class _Cut(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "5000")
        self.end_headers()
        self.wfile.write(f"{PADDING_LINE}\r\n".encode())
        self.wfile.flush()
        self.close_connection = True


@pytest.fixture
def stub() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Cut)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/range/"
    server.shutdown()
    server.server_close()


def test_a_cut_body_fails_open_with_the_warning(
    stub: str,
    settings: Any,
    breach_service: FakeBreachService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(breached, "fetch_range", breach_service.original)
    settings.PASSWORD_BREACH_API_URL = stub

    with caplog.at_level(logging.WARNING, logger="apps.accounts.breached"):
        assert not breached.is_breached("made-up-cut-Passw0rd")

    assert len(caplog.records) == 1
    assert "unavailable" in caplog.records[0].getMessage()

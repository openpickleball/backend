"""The breached password check (#100, ADR 0047).

``BreachedPasswordValidator`` is the last of ``AUTH_PASSWORD_VALIDATORS``,
so every path that sets a password runs it: signup, the signup password
check (ADR 0039), password change, password reset from a key, and the
admin's password forms.

It asks a public k-anonymity range API (``PASSWORD_BREACH_API_URL``,
Pwned Passwords by default) for the suffixes of every listed password
that shares the first five hex characters of the password's uppercase
SHA-1. Only those five characters leave OPEN: never the password, its
full hash, or its suffix. The request asks for padding (``Add-Padding:
true``), so the answer's size does not hint at the prefix; padding
entries have a count of 0 and never refuse. It carries a generic
User-Agent and no other data. ``PASSWORD_BREACH_TIMEOUT_SECONDS`` bounds
each socket wait and the body read as a whole (see ``fetch_range``).

The check fails open (the maintainer's decision on #100): on a timeout,
a connection or
protocol error, a redirect (never followed, so the prefix goes to the
configured host only), an answer other than 200, a body over
``MAX_BODY_BYTES``, or a body that is not a non-empty list of
``SUFFIX:COUNT`` lines, the password is accepted and one warning
is logged with a fixed message and the error class only. The other
password rules and the login limits still apply.

Tests never reach the service: the root ``conftest.py`` replaces
``fetch_range`` for every test (``apps/accounts/tests/breach.py``).
"""

import hashlib
import logging
import re
import time
from http.client import HTTPException, HTTPResponse
from typing import Any
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.accounts.validators import is_encodable_password

logger = logging.getLogger(__name__)

# A generic client name: no contact detail, host, or version.
USER_AGENT = "OPEN-password-check"
PREFIX_LENGTH = 5
_LINE = re.compile(r"[0-9A-F]{35}:[0-9]+")
_HTTP_OK = 200
# The most of an answer read. A padded answer is about 30 to 40 KB; a
# larger one is not the range API's and fails open.
MAX_BODY_BYTES = 1024 * 1024
_CHUNK_BYTES = 16 * 1024

BREACHED_MESSAGE = (
    "This password has appeared in a data breach, so it is not safe to use. "
    "Choose a different password."
)
BREACHED_CODE = "password_breached"


class BreachServiceError(Exception):
    """The range API answered with something other than a list of hashes."""


class _NoRedirect(HTTPRedirectHandler):
    """Never follow a redirect: the prefix goes to the configured host only.

    urllib then raises ``HTTPError`` for the 3xx answer, a service error.
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = build_opener(_NoRedirect)


def urlopen(request: Request, timeout: float) -> HTTPResponse:
    """Open ``request`` without following redirects."""
    response: HTTPResponse = _OPENER.open(request, timeout=timeout)
    return response


def fetch_range(prefix: str) -> str:
    """The range API's answer for a five character hash prefix.

    What ``PASSWORD_BREACH_TIMEOUT_SECONDS`` bounds: each socket wait
    (connecting, the TLS handshake, sending, reading the status line and
    headers, each read of the body), and the body read as a whole, which
    is read in chunks and stops at the deadline. It does not bound the
    whole request: name resolution (``getaddrinfo``) has no timeout of
    its own, and the steps before the body can each take up to the
    timeout, so a slow but live server can hold a request for a few
    times the timeout. A hard overall bound would need a thread per
    check. Raises on a timeout,
    a connection, HTTP, or protocol error, a redirect, an answer other
    than 200, a body shorter than its Content-Length, a body over
    ``MAX_BODY_BYTES``, or a body that is not ASCII.
    """
    timeout = settings.PASSWORD_BREACH_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    request = Request(
        f"{settings.PASSWORD_BREACH_API_URL}{prefix}",
        headers={"Add-Padding": "true", "User-Agent": USER_AGENT},
    )
    chunks: list[bytes] = []
    size = 0
    with urlopen(request, timeout=timeout) as response:
        if response.status != _HTTP_OK:
            raise BreachServiceError
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            chunk = response.read1(_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise BreachServiceError
            chunks.append(chunk)
        if response.length:
            # read1 ends quietly when the server closes early: bytes the
            # Content-Length promised are missing.
            raise BreachServiceError
    return b"".join(chunks).decode("ascii")


def _breach_count(body: str, suffix: str) -> int:
    """The count the answer lists for ``suffix``, 0 when it is not listed.

    A suffix listed more than once counts with its highest count. Raises
    ``BreachServiceError`` when any line is not ``SUFFIX:COUNT``, or when
    there is no line at all (the real answer always lists some).
    """
    count = 0
    lines = 0
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if not _LINE.fullmatch(line):
            raise BreachServiceError
        lines += 1
        listed, _, times = line.partition(":")
        if listed == suffix:
            count = max(count, int(times))
    if not lines:
        raise BreachServiceError
    return count


def is_breached(password: str) -> bool:
    """Whether the range API lists ``password`` with a count above 0.

    False, with one warning, when the service cannot answer (fail open).
    """
    # A password that is not UTF-8 text is refused by
    # EncodablePasswordValidator; nothing is asked for it.
    if not is_encodable_password(password):
        return False
    encoded = password.encode("utf-8")
    digest = hashlib.sha1(encoded, usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:PREFIX_LENGTH], digest[PREFIX_LENGTH:]
    try:
        count = _breach_count(fetch_range(prefix), suffix)
    except (BreachServiceError, URLError, HTTPException, OSError, ValueError) as error:
        # OSError covers timeouts and refused connections, HTTPException a
        # body cut mid-read or a broken status line, and ValueError a body
        # that is not ASCII. URLError includes a refused redirect. Never
        # the error's text: it can quote the URL, and so the prefix.
        logger.warning(
            "The breached password check is unavailable (%s); the password "
            "was accepted.",
            type(error).__name__,
        )
        return False
    return count > 0


class BreachedPasswordValidator:
    """Refuses a password the breached password service lists."""

    def validate(self, password: str, user: Any = None) -> None:
        if is_breached(password):
            raise ValidationError(BREACHED_MESSAGE, code=BREACHED_CODE)

    def get_help_text(self) -> str:
        return "Your password can't be one that has appeared in a data breach."

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
User-Agent and no other data, and waits at most
``PASSWORD_BREACH_TIMEOUT_SECONDS``.

The check fails open (the maintainer's decision on #100): on a timeout,
a connection error, an answer other than 200, or a body that is not a
list of ``SUFFIX:COUNT`` lines, the password is accepted and one warning
is logged with a fixed message and the error class only. The other
password rules and the login limits still apply.

Tests never reach the service: the root ``conftest.py`` replaces
``fetch_range`` for every test (``apps/accounts/tests/breach.py``).
"""

import hashlib
import logging
import re
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

# A generic client name: no contact detail, host, or version.
USER_AGENT = "OPEN-password-check"
PREFIX_LENGTH = 5
_LINE = re.compile(r"[0-9A-F]{35}:[0-9]+")
_HTTP_OK = 200

BREACHED_MESSAGE = (
    "This password has appeared in a data breach, so it is not safe to use. "
    "Choose a different password."
)
BREACHED_CODE = "password_breached"


class BreachServiceError(Exception):
    """The range API answered with something other than a list of hashes."""


def fetch_range(prefix: str) -> str:
    """The range API's answer for a five character hash prefix.

    Raises on a timeout, a connection or HTTP error, an answer other than
    200, or a body that is not ASCII.
    """
    request = Request(
        f"{settings.PASSWORD_BREACH_API_URL}{prefix}",
        headers={"Add-Padding": "true", "User-Agent": USER_AGENT},
    )
    with urlopen(request, timeout=settings.PASSWORD_BREACH_TIMEOUT_SECONDS) as response:
        if response.status != _HTTP_OK:
            raise BreachServiceError
        body: bytes = response.read()
    return body.decode("ascii")


def _breach_count(body: str, suffix: str) -> int:
    """The count the answer lists for ``suffix``, 0 when it is not listed.

    Raises ``BreachServiceError`` when any line is not ``SUFFIX:COUNT``.
    """
    count = 0
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if not _LINE.fullmatch(line):
            raise BreachServiceError
        listed, _, times = line.partition(":")
        if listed == suffix:
            count = int(times)
    return count


def is_breached(password: str) -> bool:
    """Whether the range API lists ``password`` with a count above 0.

    False, with one warning, when the service cannot answer (fail open).
    """
    digest = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest()
    digest = digest.upper()
    prefix, suffix = digest[:PREFIX_LENGTH], digest[PREFIX_LENGTH:]
    try:
        count = _breach_count(fetch_range(prefix), suffix)
    except (BreachServiceError, URLError, OSError, ValueError) as error:
        # OSError covers timeouts and refused connections; ValueError a
        # body that is not ASCII. Never the error's text: it can quote
        # the URL, and so the prefix.
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

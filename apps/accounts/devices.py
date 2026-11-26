"""New device detection and the new sign-in email (#100, ADR 0047).

When a login completes (``AccountAdapter.login``, after every login
stage, MFA included), OPEN reads the device marker the client sent: the
browser client's ``SIGN_IN_DEVICE_COOKIE_NAME`` cookie, or the app
client's ``X-Device-Token`` request header. The marker is a list of at
most ``MAX_DEVICE_TAGS`` tags, newest first, signed by the server
(``django.core.signing``, refused after ``SIGN_IN_DEVICE_COOKIE_DAYS``).
A tag is a keyed hash of an Account's primary key (``account_tag``):
never an email address or a row id. Nothing is stored on the server, and
no user agent, IP address, or session row is read.

When the Account had signed in before (``last_login`` was set before this
login) and the marker does not hold its tag, one email is queued
(``queue_email``) from a ``transaction.on_commit`` callback, so nothing
is sent in the request and a rolled back login queues nothing. An
Account's first sign-in sends nothing. Either way the marker is written
back with this Account's tag first: as the cookie (HttpOnly, SameSite
Lax, Secure and ``__Host-`` with DEBUG off) for the browser client, or
in the ``X-Device-Token`` response header for the app client, which
sends it back with its next login's requests.

The email names the time, in UTC to the minute, and the client type, and
links to the web app's session list. It holds no IP address, user agent,
location, request host, or recipient address.
"""

import logging
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Literal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.http import HttpRequest, HttpResponseBase
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.emails.rendering import render_email
from apps.emails.services import queue_email

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

logger = logging.getLogger(__name__)

Client = Literal["browser", "app"]

NEW_SIGN_IN = "accounts/email/new_sign_in"
DEVICE_TOKEN_HEADER = "X-Device-Token"
MAX_DEVICE_TAGS = 10
_TAG_LENGTH = 32
_TAG_SALT = "apps.accounts.devices.account_tag"
_MARKER_SALT = "apps.accounts.devices.marker"
# Where a completed login leaves the marker for the response.
_MARKER_ATTRIBUTE = "open_device_marker"
_CLIENT_NAMES: dict[str, str] = {"browser": "a web browser", "app": "the OPEN app"}


def account_tag(account: "_User") -> str:
    """The marker's tag for ``account``: a keyed hash of its primary key."""
    digest: str = salted_hmac(
        _TAG_SALT, str(account.pk), algorithm="sha256"
    ).hexdigest()
    return digest[:_TAG_LENGTH]


def read_tags(value: str | None) -> list[str]:
    """The tags in a marker, or none when it is missing, changed, or old."""
    if not value:
        return []
    try:
        tags = signing.loads(
            value,
            salt=_MARKER_SALT,
            max_age=settings.SIGN_IN_DEVICE_COOKIE_DAYS * 86400,
        )
    except signing.BadSignature:
        return []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        return []
    return tags[:MAX_DEVICE_TAGS]


def _marker(tags: list[str]) -> str:
    return signing.dumps(tags[:MAX_DEVICE_TAGS], salt=_MARKER_SALT)


def _client(request: HttpRequest) -> Client:
    headless = getattr(getattr(request, "allauth", None), "headless", None)
    client = getattr(getattr(headless, "client", None), "value", None)
    return "app" if client == "app" else "browser"


def _sent_marker(request: HttpRequest, client: Client) -> str | None:
    if client == "app":
        return request.headers.get(DEVICE_TOKEN_HEADER)
    return request.COOKIES.get(settings.SIGN_IN_DEVICE_COOKIE_NAME)


def note_sign_in(
    request: HttpRequest, account: "_User", signed_in_before: bool
) -> None:
    """At a completed login: alert a new device, then mark this one.

    ``signed_in_before`` is whether the Account's ``last_login`` was set
    before this login.
    """
    client = _client(request)
    tags = read_tags(_sent_marker(request, client))
    tag = account_tag(account)
    if signed_in_before and tag not in tags:
        transaction.on_commit(
            partial(send_sign_in_alert, account.pk, client, timezone.now().isoformat())
        )
    marked = [tag, *(other for other in tags if other != tag)]
    setattr(request, _MARKER_ATTRIBUTE, (client, _marker(marked)))


def write_marker(request: HttpRequest, response: HttpResponseBase) -> None:
    """Put the marker a completed login left on ``request`` on ``response``."""
    marker: tuple[Client, str] | None = getattr(request, _MARKER_ATTRIBUTE, None)
    if marker is None:
        return
    client, value = marker
    if client == "app":
        response[DEVICE_TOKEN_HEADER] = value
        return
    response.set_cookie(
        settings.SIGN_IN_DEVICE_COOKIE_NAME,
        value,
        max_age=settings.SIGN_IN_DEVICE_COOKIE_DAYS * 86400,
        path="/",
        secure=settings.SIGN_IN_DEVICE_COOKIE_SECURE,
        httponly=True,
        samesite="Lax",
    )


def _minute(at: datetime) -> str:
    """``at`` in UTC to the minute, such as ``26 November 2026 at 14:05 UTC``."""
    utc = at.astimezone(UTC)
    return f"{utc.day} {utc:%B %Y at %H:%M} UTC"


def render_sign_in_alert(
    account: "_User", *, client: Client, at: datetime
) -> EmailMultiAlternatives:
    """The new sign-in email to ``account``'s login address."""
    return render_email(
        NEW_SIGN_IN,
        str(account.email),
        {
            "client": _CLIENT_NAMES[client],
            "signed_in_at": _minute(at),
            "sessions_url": f"{settings.FRONTEND_URL}/account/sessions",
        },
    )


def send_sign_in_alert(account_pk: int, client: Client, at: str) -> None:
    """Queue the new sign-in email (an on_commit callback of the login).

    It reads the Account again and sends nothing to an inactive one. A
    failure is logged with the error class only and never fails the
    login, which has already committed.
    """
    try:
        account = get_user_model()._default_manager.filter(pk=account_pk).first()
        if account is None or not account.is_active:
            return
        message = render_sign_in_alert(
            account, client=client, at=datetime.fromisoformat(at)
        )
        queue_email(message, template=NEW_SIGN_IN)
    except Exception as error:
        logger.error("Could not queue a new sign-in email (%s).", type(error).__name__)

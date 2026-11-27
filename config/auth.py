"""Account authentication for OPEN API operations (issue #30).

`account_auth` accepts either of allauth's two headless clients:

- the app client's session token, sent in the `X-Session-Token` header
  (no CSRF, since no cookie is involved), and
- the browser client's Django session cookie, with CSRF checked on unsafe
  methods (`SessionAuth`, a subclass of ninja's).

Pass it as an operation's or router's `auth`. The signed-in Account is in
`request.auth` (and `request.user`) as a plain Account instance, never
Django's lazy proxy, so a view can pass it straight to a service that
reads the actor's model class. A request that
neither one authenticates gets a 401 `ErrorResponse`
(`config/api_errors.py`), whatever its method. A signed-in session's
unsafe request without a valid CSRF token gets 403.

An Account that must have two-factor authentication
(``apps.accounts.mfa.mfa_required``: staff, superusers, moderators, and
owners of a production Partner Application) and has no method
(``has_mfa``), both read in one query (``mfa_status``), is refused with ``MFARequired``, a 403 with code
``mfa_required`` (issue #99, ADR 0043), on every operation either class
protects. It can still sign in and use allauth's endpoints, where it sets
up a method. An Account with a method counts only with a session that
passed the second factor (``session_passed_mfa``): a password-only
session, such as one made before the Account enrolled, gets the same
403. The check runs after authentication, so a request with no
Account still gets 401, and for a browser session after ninja's CSRF
check, so an unsafe request without its token still gets the CSRF 403.

`require_step_up(request)` (#232, ADR 0057) refuses an operation with
403 ``reauthentication_required`` (``STEP_UP_MESSAGE`` for an Account with
MFA, the general reauthentication text for one without) unless the
session behind the request took a second factor step (any
authentication step, for an Account without MFA) within
``ORGANIZER_STEP_UP_SECONDS``. Organizer and Club admin changes and the
results file upload call it.

`auth_session(request)` returns the session behind the request's
authentication: the session the `X-Session-Token` names for the app
client, `request.session` for the browser client. A view that reads or
changes a session value (the signup details, ADR 0017) uses it. The browser
session is saved by Django's middleware; a view that changes the token
session saves it itself (`save()` keeps its key, so the token still works).
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from allauth.account.internal.flows.reauthentication import (
    did_recently_reauthenticate,
)
from allauth.headless.contrib.ninja.security import XSessionTokenAuth
from allauth.headless.internal.sessionkit import authenticate_by_x_session_token
from allauth.usersessions.internal import usersessionkit
from django.conf import settings
from django.contrib.sessions.backends.base import SessionBase
from django.http import HttpRequest
from ninja.errors import HttpError
from ninja.security import SessionAuth as NinjaSessionAuth

from apps.accounts.mfa import (
    has_mfa,
    last_authenticated_at,
    last_mfa_at,
    mfa_status,
    session_passed_mfa,
)

# Where SessionTokenAuth keeps the token's session on the request.
_TOKEN_SESSION_ATTRIBUTE = "open_token_session"

MFA_REQUIRED_STATUS = 403
MFA_REQUIRED_MESSAGE = (
    "Two-factor authentication is required: set it up, or sign in with it, to continue."
)


class MFARequired(HttpError):
    """The Account must set up two-factor authentication first (403).

    Fixed text: never which rule made it required.
    """

    def __init__(self) -> None:
        super().__init__(MFA_REQUIRED_STATUS, MFA_REQUIRED_MESSAGE)


REAUTHENTICATION_REQUIRED_STATUS = 403
REAUTHENTICATION_REQUIRED_MESSAGE = (
    "Confirm your password or a two-factor code again, then retry."
)


STEP_UP_MESSAGE = "Confirm a two-factor code again, then retry."


class ReauthenticationRequired(HttpError):
    """The operation needs a recent reauthentication (403, #116).

    Fixed text. The client reauthenticates through allauth
    (``auth/reauthenticate`` with the password, or ``auth/2fa/reauthenticate``
    with a code) and sends the request again. The step-up (#232) passes
    ``STEP_UP_MESSAGE``, since only a code satisfies it for an Account
    with MFA.
    """

    def __init__(self, message: str = REAUTHENTICATION_REQUIRED_MESSAGE) -> None:
        super().__init__(REAUTHENTICATION_REQUIRED_STATUS, message)


def recently_reauthenticated(request: HttpRequest) -> bool:
    """Whether the request's Account reauthenticated within
    ``ACCOUNT_REAUTHENTICATION_TIMEOUT``.

    allauth's own check, run on the session behind the request's
    authentication (``auth_session``), so it works for both clients: the
    app client's reauthentication is recorded in its token's session. A
    login alone does not count, only allauth's reauthentication flows.
    """
    view = SimpleNamespace(user=request.auth, session=auth_session(request))  # type: ignore[attr-defined]
    return bool(did_recently_reauthenticate(cast("HttpRequest", view)))


def require_recent_reauthentication(request: HttpRequest) -> None:
    """Raise ``ReauthenticationRequired`` unless ``recently_reauthenticated``."""
    if not recently_reauthenticated(request):
        raise ReauthenticationRequired


def stepped_up(request: HttpRequest) -> bool:
    """Whether the request's session took a recent enough step (#232).

    For an Account with MFA, its latest second factor step (an MFA login,
    an MFA reauthentication, or enrollment: ``last_mfa_at``); for one
    without, its latest authentication record of any kind
    (``last_authenticated_at``). Recent means within
    ``ORGANIZER_STEP_UP_SECONDS``, read from the session behind the
    request (``auth_session``), so both clients work.
    """
    return _stepped_up(request, has_mfa(request.auth))  # type: ignore[attr-defined]


def _stepped_up(request: HttpRequest, enrolled: bool) -> bool:
    account = request.auth  # type: ignore[attr-defined]
    session = auth_session(request)
    if enrolled:
        last = last_mfa_at(session, account)
    else:
        last = last_authenticated_at(session, account)
    return last is not None and time.time() - last <= settings.ORGANIZER_STEP_UP_SECONDS


def require_step_up(request: HttpRequest) -> None:
    """Raise ``ReauthenticationRequired`` unless ``stepped_up``: with
    ``STEP_UP_MESSAGE`` for an Account with MFA, and the general
    ``REAUTHENTICATION_REQUIRED_MESSAGE`` for one without, which has no
    code to give. Call it before any lookup of the path's records, so the
    answer is the same for every well-formed code."""
    enrolled = has_mfa(request.auth)  # type: ignore[attr-defined]
    if not _stepped_up(request, enrolled):
        raise ReauthenticationRequired(
            STEP_UP_MESSAGE if enrolled else REAUTHENTICATION_REQUIRED_MESSAGE
        )


def require_mfa(account: Any, session: SessionBase | None) -> Any:
    """``account``, or ``MFARequired`` (ADR 0043).

    An Account with a method counts only with a ``session`` that passed
    the second factor (``session_passed_mfa``). One without a method is
    refused when it needs one (``mfa_required``). One query.
    """
    status = mfa_status(account)
    if status.enrolled:
        if session is None or not session_passed_mfa(session, account):
            raise MFARequired
    elif status.required:
        raise MFARequired
    return account


class SessionTokenAuth(XSessionTokenAuth):
    """allauth's `X-Session-Token` auth, documented as a header API key.

    allauth's class names only the scheme type, which leaves its OpenAPI
    security scheme without the required `in` and `name`.
    """

    openapi_in: str = "header"
    openapi_name: str = "X-Session-Token"

    def __call__(self, request: HttpRequest) -> Any | None:
        """allauth's check, keeping the token's session on the request.

        The same steps as allauth's ``__call__`` (with anonymous users and
        invalid tokens refused, its defaults), plus the session, which
        allauth's class drops. Set only when the token authenticates.
        """
        token = self.get_session_token(request)
        if not token:
            return None
        result = authenticate_by_x_session_token(token)
        if result is None or result.user is None:
            return None
        require_mfa(result.user, result.session)
        usersessionkit.track_activity(request, user=result.user, session=result.session)
        request.user = result.user
        setattr(request, _TOKEN_SESSION_ATTRIBUTE, result.session)
        return result.user


class SessionAuth(NinjaSessionAuth):
    """ninja's session cookie auth, which checks CSRF only for a session.

    ninja's class checks CSRF before it looks for a signed-in user, so an
    unsafe request with no session at all is refused as a CSRF failure
    (403) instead of as unauthenticated (401). This one returns None when
    the request has no signed-in user (Django's auth backends leave an
    inactive Account's session signed out), and runs ninja's check, CSRF
    included, only when it has one. The name stays `SessionAuth`, the
    OpenAPI security scheme's name.
    """

    def __call__(self, request: HttpRequest) -> Any | None:
        if not request.user.is_authenticated:
            return None
        return super().__call__(request)

    def authenticate(self, request: HttpRequest, key: str | None) -> Any | None:
        """The signed-in Account itself, not Django's lazy ``request.user``.

        ``__call__`` already read ``is_authenticated``, which loaded the
        Account behind the proxy, so unwrapping it needs no query. It runs
        only for a signed-in user, after ninja's CSRF check.
        """
        return require_mfa(
            getattr(request.user, "_wrapped", request.user),
            getattr(request, "session", None),
        )


account_auth = [SessionTokenAuth(), SessionAuth()]


def auth_session(request: HttpRequest) -> SessionBase:
    """The session behind the request's authentication.

    The token's session when ``SessionTokenAuth`` authenticated the
    request, otherwise ``request.session`` (the browser client's cookie).
    """
    session: SessionBase | None = getattr(request, _TOKEN_SESSION_ATTRIBUTE, None)
    return session if session is not None else request.session

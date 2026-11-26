"""A player's sessions: listing, ending, and the sweep (#100, ADR 0047).

allauth's user sessions app (``allauth.usersessions``) keeps one
``UserSession`` row per signed-in session, created at login, with OPEN's
client IP (``AccountAdapter.get_client_ip``) and the user agent. Rows
last only while their session does: logout deletes the row
(``forget_session``), ending a session deletes it, and the sweep task
deletes the rows of sessions that expired or are gone.

Ids. allauth lists each row by its primary key and ends sessions by
those keys. OPEN's sessions view uses ``public_session_id`` instead, a
keyed hash of the session key, so no row id reaches the API (CLAUDE.md,
Public identifiers) and the session key, which is the app client's
token, cannot be recovered from it.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING

from allauth.usersessions.internal.flows import sessions as allauth_sessions
from allauth.usersessions.models import UserSession
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.audit.models import AuditAction
from apps.audit.services import record_event

if TYPE_CHECKING:
    from django.contrib.auth.models import _User
    from django.http import HttpRequest

_ID_SALT = "apps.accounts.sessions.public_session_id"
# Hex characters of the keyed hash: 128 bits.
_ID_LENGTH = 32


def public_session_id(user_session: UserSession) -> str:
    """The id the API lists ``user_session`` by: a keyed hash of its key."""
    digest: str = salted_hmac(
        _ID_SALT, user_session.session_key, algorithm="sha256"
    ).hexdigest()
    return digest[:_ID_LENGTH]


def sessions_by_public_id(account: "_User") -> dict[str, UserSession]:
    """The Account's user sessions, keyed by their public ids."""
    return {
        public_session_id(row): row
        for row in UserSession.objects.filter(user_id=account.pk)
    }


def end_sessions_by_player(
    request: "HttpRequest", account: "_User", sessions: Iterable[UserSession]
) -> None:
    """End ``sessions``, which the player chose, recording each.

    One ``session.end`` Audit Event per session, actor and target the
    Account, ``after`` only whether it was the current session: never
    its IP address, user agent, or key. Call it inside the change's
    ``transaction.atomic()``. Ending the current session logs it out.
    """
    chosen = list(sessions)
    for row in chosen:
        record_event(
            action=AuditAction.SESSION_END,
            actor=account,
            target=account,
            after={"current": row.is_current()},
        )
    allauth_sessions.end_sessions(request, chosen)


def end_other_sessions(request: "HttpRequest", account: "_User") -> None:
    """End every session of ``account`` but the request's (allauth's flow).

    Only sessions with a ``UserSession`` row: one made before user
    sessions were installed has none. A password change also ends those,
    through Django's session auth hash.
    """
    allauth_sessions.end_other_sessions(request, account)


def end_all_sessions(request: "HttpRequest", account: "_User") -> None:
    """End every session of ``account`` that has a ``UserSession`` row."""
    allauth_sessions.end_sessions(
        request, list(UserSession.objects.filter(user_id=account.pk))
    )


def forget_session(session_key: str | None) -> None:
    """Delete the ``UserSession`` row of a session that ended, if any."""
    if session_key:
        UserSession.objects.filter(session_key=session_key).delete()


def sweep_user_sessions() -> int:
    """Delete expired sessions, then every ``UserSession`` row whose
    session is gone or expired. Returns how many rows it deleted.

    Sessions use Django's database engine (the default ``SESSION_ENGINE``).
    """
    now = timezone.now()
    Session.objects.filter(expire_date__lte=now).delete()
    live = Session.objects.filter(expire_date__gt=now).values("session_key")
    deleted, _ = UserSession.objects.exclude(session_key__in=live).delete()
    return int(deleted)

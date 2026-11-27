"""A player's sessions: lifetimes, listing, ending, and the sweep (#100,
ADR 0047).

Lifetimes. A signed-in session, a browser's cookie session or an app's
session token (the token is the session's key), ends
``SESSION_MAX_AGE_DAYS`` after login or ``SESSION_IDLE_DAYS`` after it
was last seen, whichever comes first. Both times live in the session
itself (``SESSION_STARTED_KEY``, ``SESSION_SEEN_KEY``, whole seconds
since the epoch), so there is no table. Last seen is written at most once
per ``SESSION_SEEN_REFRESH_SECONDS``, so a request rarely writes. Each
write also sets the session's own expiry (``set_expiry``) to the earlier
of the two ends, so Django's store refuses the session at that time
whatever the path, and the sweep deletes it. ``check_lifetime`` runs on
every path that reads a signed-in session: the browser middleware
(``SessionLifetimeMiddleware``), and the app token lookup
(``apps.accounts.token_strategy``), which both allauth's endpoints and
the OPEN API use. A signed-in session that has no times yet (made before
#100) gets them, as if it had just signed in.

An Account with any Organizer membership or Club Role uses the shorter
``ORGANIZER_SESSION_MAX_AGE_DAYS`` and ``ORGANIZER_SESSION_IDLE_DAYS``
(#232, ADR 0057). Whether it does is stored in the session
(``SESSION_SHORT_KEY``), read from the database at login and again when
last seen moves or the value is missing, never on every request. So a
membership gained mid-session applies at the next refresh, at most
``SESSION_SEEN_REFRESH_SECONDS`` later.

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

import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from allauth.usersessions.internal.flows import sessions as allauth_sessions
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.base import SessionBase
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.audit.models import AuditAction
from apps.audit.services import record_event

if TYPE_CHECKING:
    from django.contrib.auth.models import _User
    from django.http import HttpRequest

# When the session signed in and when it was last seen, in whole seconds
# since the epoch.
SESSION_STARTED_KEY = "open_session_started"
SESSION_SEEN_KEY = "open_session_seen"
# Whether the session's Account has an Organizer membership or a Club
# Role, so the shorter member lifetimes apply (#232). A bool.
SESSION_SHORT_KEY = "open_session_short"
_DAY = 86400

_ID_SALT = "apps.accounts.sessions.public_session_id"
# Hex characters of the keyed hash: 128 bits.
_ID_LENGTH = 32


def _ends(started: int, seen: int, *, short: bool = False) -> int:
    """When a session with these times ends, in seconds since the epoch.

    ``short`` picks the Organizer member and Club admin lifetimes (#232).
    """
    if short:
        max_age, idle = (
            settings.ORGANIZER_SESSION_MAX_AGE_DAYS,
            settings.ORGANIZER_SESSION_IDLE_DAYS,
        )
    else:
        max_age, idle = settings.SESSION_MAX_AGE_DAYS, settings.SESSION_IDLE_DAYS
    return min(started + max_age * _DAY, seen + idle * _DAY)


def _is_short(session: SessionBase) -> bool:
    return session.get(SESSION_SHORT_KEY) is True


def _set_times(session: SessionBase, started: int, seen: int) -> None:
    session[SESSION_STARTED_KEY] = started
    session[SESSION_SEEN_KEY] = seen
    session.set_expiry(
        datetime.fromtimestamp(_ends(started, seen, short=_is_short(session)), tz=UTC)
    )


def _holds_access_role(session: SessionBase) -> bool:
    """Whether the session's Account has any Organizer membership or Club
    Role, read from the database (#232). False with no Account."""
    # Imported here: apps.clubs imports this app's modules.
    from apps.clubs.models import ClubRole, OrganizerMember

    account_id = session.get(SESSION_KEY)
    if account_id is None:
        return False
    try:
        return bool(
            OrganizerMember.objects.filter(account_id=account_id).exists()
            or ClubRole.objects.filter(account_id=account_id).exists()
        )
    except (TypeError, ValueError):
        return False


def _note_short(session: SessionBase) -> None:
    session[SESSION_SHORT_KEY] = _holds_access_role(session)


def start_lifetime(session: SessionBase) -> None:
    """Start a signed-in session's clocks: at login. Notes whether the
    Account uses the shorter member lifetimes."""
    _note_short(session)
    now = int(time.time())
    _set_times(session, now, now)


def check_lifetime(session: SessionBase) -> bool:
    """Whether ``session`` may still be used: False once it has ended.

    A session that is not signed in is not checked. One with no times, or
    times that are not whole numbers, gets fresh ones. Last seen moves
    when it is ``SESSION_SEEN_REFRESH_SECONDS`` old or more, which marks
    the session modified: the caller saves it. Whether the session is
    short is read from the database again then, and whenever the stored
    value is missing or not a bool, so a membership gained mid-session
    applies at the next refresh (#232).
    """
    if SESSION_KEY not in session:
        return True
    started = session.get(SESSION_STARTED_KEY)
    seen = session.get(SESSION_SEEN_KEY)
    if not isinstance(started, int) or not isinstance(seen, int):
        start_lifetime(session)
        return True
    noted = not isinstance(session.get(SESSION_SHORT_KEY), bool)
    if noted:
        _note_short(session)
    now = int(time.time())
    if now >= _ends(started, seen, short=_is_short(session)):
        return False
    if now - seen >= settings.SESSION_SEEN_REFRESH_SECONDS:
        if not noted:
            _note_short(session)
        _set_times(session, started, now)
        if now >= _ends(started, now, short=_is_short(session)):
            # A membership gained since makes the session already ended.
            return False
    elif noted:
        _set_times(session, started, seen)
    return True


def end_session(session: SessionBase) -> None:
    """Delete an ended session and its ``UserSession`` row."""
    key = session.session_key
    session.flush()
    forget_session(key)


def public_session_id(user_session: UserSession) -> str:
    """The id the API lists ``user_session`` by: a keyed hash of its key."""
    digest: str = salted_hmac(
        _ID_SALT, user_session.session_key, algorithm="sha256"
    ).hexdigest()
    return digest[:_ID_LENGTH]


def sessions_by_public_id(account: "_User") -> dict[str, UserSession]:
    """The Account's live user sessions, keyed by their public ids.

    Rows of sessions that are gone are purged first, so only a live
    session can be ended and audited.
    """
    return {
        public_session_id(row): row
        for row in UserSession.objects.purge_and_list(account)
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


def end_sessions_after_password_change(
    request: "HttpRequest", account: "_User", old_key: str | None
) -> None:
    """End sessions after a password change outside allauth (the admin).

    Django cycles the acting session's key in both cases (its set-password
    view does so even for another Account's password), so the acting
    session's row first follows the new key. Then, for the Account's own
    change, its other sessions end; for staff setting another Account's
    password, every session of that Account ends. Call it inside the
    change's ``transaction.atomic()``.
    """
    session = getattr(request, "session", None)
    key = getattr(session, "session_key", None)
    if old_key and key and old_key != key:
        UserSession.objects.filter(user_id=request.user.pk, session_key=old_key).update(
            session_key=key
        )
    if getattr(request.user, "pk", None) == account.pk:
        end_other_sessions(request, account)
    else:
        end_all_sessions(request, account)


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

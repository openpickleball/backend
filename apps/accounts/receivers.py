"""Signal receivers for Accounts (#100), connected in ``AccountsConfig``."""

from typing import Any

from django.http import HttpRequest

from apps.accounts.sessions import forget_session


def forget_session_on_logout(
    sender: object, request: HttpRequest | None = None, **kwargs: Any
) -> None:
    """Delete the logged out session's ``UserSession`` row (its IP address
    and user agent) right away, not at the next sweep. Django sends
    ``user_logged_out`` before it flushes the session."""
    session = getattr(request, "session", None)
    forget_session(getattr(session, "session_key", None))

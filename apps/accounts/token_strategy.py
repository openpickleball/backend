"""allauth's session token strategy, with OPEN's lifetimes (#100).

``HEADLESS_TOKEN_STRATEGY``. The app client's ``X-Session-Token`` is the
session's key. allauth's endpoints and the OPEN API (``config/auth.py``,
through ``authenticate_by_x_session_token``) both look a token up here,
so both apply the lifetimes (``apps.accounts.sessions.check_lifetime``).
A token past its lifetime is deleted, with its user session row, and
looks unknown: allauth answers 410, the OPEN API 401.
"""

from allauth.headless.tokens.strategies.sessions import (
    SessionTokenStrategy as BaseSessionTokenStrategy,
)
from django.contrib.sessions.backends.base import SessionBase

from apps.accounts.sessions import check_lifetime, end_session


class SessionTokenStrategy(BaseSessionTokenStrategy):
    def lookup_session(self, session_token: str) -> SessionBase | None:
        session: SessionBase | None = super().lookup_session(session_token)
        if session is None:
            return None
        if not check_lifetime(session):
            end_session(session)
            return None
        if session.modified:
            # Last seen moved, or clocks were added: save it under its key.
            session.save()
        return session

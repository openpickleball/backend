"""allauth's session token strategy, with OPEN's lifetimes (#100).

``HEADLESS_TOKEN_STRATEGY``. The app client's ``X-Session-Token`` is the
session's key. allauth's endpoints and the OPEN API (``config/auth.py``,
through ``authenticate_by_x_session_token``) both look a token up here,
so both apply the lifetimes (``apps.accounts.sessions.check_lifetime``).

Two ways a token's session ends:

- In real time, the store's own expiry (set to the same instant the
  lifetime check compares) refuses the session first. The token then
  names an empty, signed-out session: allauth answers 401 and the OPEN
  API 401. A token whose session is not signed in, or is gone, has no
  user session row to keep, so its row (if any) is deleted here.
- When the store still holds a signed-in session past its lifetime (one
  whose store expiry is later, such as a session from before #100), the
  check ends it: the session and its row are deleted and the token looks
  unknown, so allauth answers 410 and the OPEN API 401.
"""

from allauth.headless.tokens.strategies.sessions import (
    SessionTokenStrategy as BaseSessionTokenStrategy,
)
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.base import SessionBase, UpdateError

from apps.accounts.sessions import check_lifetime, end_session, forget_session


class SessionTokenStrategy(BaseSessionTokenStrategy):
    def lookup_session(self, session_token: str) -> SessionBase | None:
        session: SessionBase | None = super().lookup_session(session_token)
        if session is None:
            forget_session(session_token)
            return None
        if not check_lifetime(session):
            end_session(session)
            return None
        if session.modified:
            # Last seen moved, or clocks were added: save it under its key.
            try:
                session.save()
            except UpdateError:
                # A logout deleted the session meanwhile: no session.
                forget_session(session_token)
                return None
        if SESSION_KEY not in session:
            # Expired in the store (it loads empty), or never signed in:
            # no user session row may outlive a signed-in session.
            forget_session(session_token)
        return session

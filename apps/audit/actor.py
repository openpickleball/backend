"""The Account acting in the current request, for Audit Events.

Model hooks that record an Audit Event (``apps.audit.access``) are not
handed an actor by their caller (the admin saves inline rows itself), so
the request's Account is held here. ``AuditActorMiddleware`` sets it for
every request, and code outside a request can set it with ``acting_as()``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser

_actor: ContextVar[Any] = ContextVar("audit_actor", default=None)


@contextmanager
def acting_as(actor: Any) -> Iterator[None]:
    """Make ``actor`` the current actor inside the block.

    ``actor`` may be an Account, an anonymous user, a lazy ``request.user``,
    or None. The previous actor comes back when the block ends.
    """
    token = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(token)


def current_actor() -> AbstractBaseUser | None:
    """The current actor when it is a signed-in Account, otherwise None."""
    actor = _actor.get()
    if actor is None or not actor.is_authenticated:
        return None
    account: AbstractBaseUser = actor
    return account

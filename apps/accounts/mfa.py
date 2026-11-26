"""Two-factor authentication rules (issue #99, ADR 0043).

allauth.mfa stores each Account's authenticators. These helpers answer
the questions OPEN's rules ask of them, always from the database by the
Account's id, never from an instance cache:

- ``has_mfa``: the Account has a method, TOTP (or WebAuthn, once #117
  turns it on). Recovery codes alone never count: they are a fallback
  for a method, not one.
- ``accounts_with_mfa``: the ids of those Accounts, for ``pk__in=`` and
  ``account_id__in=`` filters in queries that mirror ``has_mfa``.
- ``mfa_required``: the Account must have a method before it uses the
  API (``config/auth.py``): staff, superusers, moderators, and owners of
  a production (non-sandbox) Partner Application. The admin needs a
  method for every staff Account (``config/admin.py``), and Organizer
  authority needs one for every member (``apps.matches.permissions``).
- ``session_passed_mfa``: the session's login passed the second factor.
  No rule uses it yet; it is the check for #101, #117, and #232.
- ``record_mfa_change``: the one Audit Event per MFA change, written in
  the change's transaction (``apps/accounts/mfa_views.py``).
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, cast

from allauth.account.authentication import get_authentication_records
from allauth.mfa.models import Authenticator
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.base import SessionBase
from django.db.models import QuerySet
from oauth2_provider.models import get_application_model

from apps.audit.models import AuditAction
from apps.audit.services import record_event

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

    from apps.partners.models import PartnerApplication

# The authenticator types that are a method. Recovery codes are not.
MFA_METHOD_TYPES: tuple[str, ...] = (
    Authenticator.Type.TOTP,
    Authenticator.Type.WEBAUTHN,
)

MFAChange = Literal["added", "removed", "regenerated", "used"]


def _saved_account_pk(actor: object) -> int | None:
    """The row id of a saved Account, or None for anything else."""
    if not isinstance(actor, get_user_model()):
        return None
    pk: int | None = actor.pk
    return pk


def has_mfa(actor: object) -> bool:
    """True when ``actor`` is a saved Account with a method (TOTP)."""
    pk = _saved_account_pk(actor)
    if pk is None:
        return False
    found: bool = Authenticator.objects.filter(
        user_id=pk, type__in=MFA_METHOD_TYPES
    ).exists()
    return found


def accounts_with_mfa() -> QuerySet[Authenticator, dict[str, int]]:
    """The ``user_id`` of every Account with a method, as a subquery."""
    ids: QuerySet[Authenticator, dict[str, int]] = Authenticator.objects.filter(
        type__in=MFA_METHOD_TYPES
    ).values("user_id")
    return ids


def mfa_required(actor: object) -> bool:
    """True when ``actor`` must have a method before using the API.

    A saved Account that is staff, a superuser, a moderator
    (``matches.moderate_match``), or the owner of a Partner Application
    with production access (``is_sandbox`` false, #29). All read again
    from the database by id.
    """
    # Imported here: apps.matches.permissions imports this module.
    from apps.matches.permissions import MODERATE_PERMISSION

    pk = _saved_account_pk(actor)
    if pk is None:
        return False
    fresh = get_user_model().objects.filter(pk=pk).first()
    if fresh is None:
        return False
    if fresh.is_staff or fresh.is_superuser or fresh.has_perm(MODERATE_PERMISSION):
        return True
    application_model = cast("type[PartnerApplication]", get_application_model())
    owns_production_application: bool = application_model._default_manager.filter(
        user_id=pk, is_sandbox=False
    ).exists()
    return owns_production_application


def session_passed_mfa(session: SessionBase, account: "_User") -> bool:
    """True when ``session`` holds a second factor record for ``account``.

    allauth records each authentication step of a session, bound to the
    Account that took it. A login that passed the MFA stage, or an MFA
    reauthentication, leaves a record with method ``mfa``.
    """
    records = get_authentication_records(SimpleNamespace(session=session), account)
    return any(record.get("method") == "mfa" for record in records)


def record_mfa_change(account: "_User", change: MFAChange, kind: str) -> None:
    """Record one ``mfa.change`` Audit Event for ``account``.

    ``kind`` is the authenticator type (``totp`` or ``recovery_codes``).
    Never a secret, a code, or a seed. Call it inside the change's
    ``transaction.atomic()``.
    """
    record_event(
        action=AuditAction.MFA_CHANGE,
        actor=account,
        target=account,
        after={"change": change, "type": kind},
    )

"""Two-factor authentication rules (issue #99, ADR 0043).

allauth.mfa stores each Account's authenticators. These helpers answer
the questions OPEN's rules ask of them, always from the database by the
Account's id, never from an instance cache:

- ``has_mfa``: the Account has a method, TOTP or a passkey (WebAuthn,
  #117). Recovery codes alone never count: they are a fallback for a
  method, not one.
- ``has_totp``: the Account has a TOTP authenticator. The admin needs
  this method in particular, since its login script takes only codes
  (``config/admin.py``, ADR 0056); a passkey does not meet it there.
- ``accounts_with_mfa``: the ids of those Accounts, for ``pk__in=`` and
  ``account_id__in=`` filters in queries that mirror ``has_mfa``.
- ``mfa_required``: the Account must have a method before it uses the
  API (``config/auth.py``): staff, superusers, moderators, and owners of
  a production (non-sandbox) Partner Application. The admin needs a
  method for every staff Account (``config/admin.py``), and Organizer
  authority needs one for every member (``apps.matches.permissions``).
  ``lacks_required_mfa`` asks both questions in one query, and
  ``mfa_status`` returns both answers in one query.
- ``session_passed_mfa``: the session passed the second factor (an MFA
  login, a passkey login, an MFA reauthentication, or enrolling in it).
  The API
  (``config/auth.py``) and the MFA management views
  (``apps/accounts/mfa_views.py``) need it for an Account with MFA, so a
  password-only session (one made before the Account enrolled, or by a
  password login outside allauth's MFA stage, such as Django's own) gets
  no MFA-protected access. A login that stops after the password, at the
  MFA stage, is not signed in at all. The admin needs it too
  (``config/admin.py``).
- ``last_mfa_at``: when the session last passed the second factor, for
  the admin's session and reauthentication limits (``config/admin.py``)
  and the Organizer step-up (``config/auth.py``, #232).
- ``last_authenticated_at``: when the session last took any
  authentication step, for the step-up of an Account without MFA.
- ``record_mfa_change``: the one Audit Event per MFA change, written in
  the change's transaction (``apps/accounts/mfa_views.py``).
"""

import math
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, cast

from allauth.account.authentication import get_authentication_records
from allauth.mfa.models import Authenticator
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.backends.base import SessionBase
from django.db.models import (
    BooleanField,
    Exists,
    ExpressionWrapper,
    OuterRef,
    Q,
    QuerySet,
)
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
    """True when ``actor`` is a saved Account with a method (TOTP or a
    passkey)."""
    pk = _saved_account_pk(actor)
    if pk is None:
        return False
    found: bool = Authenticator.objects.filter(
        user_id=pk, type__in=MFA_METHOD_TYPES
    ).exists()
    return found


def has_totp(actor: object) -> bool:
    """True when ``actor`` is a saved Account with a TOTP authenticator,
    read from the database by id. The admin's rule (ADR 0056)."""
    pk = _saved_account_pk(actor)
    if pk is None:
        return False
    found: bool = Authenticator.objects.filter(
        user_id=pk, type=Authenticator.Type.TOTP
    ).exists()
    return found


def accounts_with_mfa() -> QuerySet[Authenticator, dict[str, int]]:
    """The ``user_id`` of every Account with a method, as a subquery."""
    ids: QuerySet[Authenticator, dict[str, int]] = Authenticator.objects.filter(
        type__in=MFA_METHOD_TYPES
    ).values("user_id")
    return ids


def _requires_mfa() -> Q:
    """The rule ``mfa_required`` applies, as a filter on Accounts.

    Staff, a superuser, a moderator (an active Account holding
    ``matches.moderate_match`` itself or through a group, as Django's
    ``ModelBackend.has_perm`` reads it), or the owner of a Partner
    Application with production access (``is_sandbox`` false, #29).
    """
    # Imported here: apps.matches.permissions imports this module.
    from apps.matches.permissions import MODERATE_PERMISSION

    account_model = get_user_model()
    app_label, codename = MODERATE_PERMISSION.split(".")
    permission = Permission.objects.filter(
        content_type__app_label=app_label, codename=codename
    ).values("pk")
    own_permission = account_model.user_permissions.through.objects.filter(
        user_id=OuterRef("pk"), permission_id__in=permission
    )
    group_permission = account_model.groups.through.objects.filter(
        user_id=OuterRef("pk"),
        group_id__in=Group.permissions.through.objects.filter(
            permission_id__in=permission
        ).values("group_id"),
    )
    application_model = cast("type[PartnerApplication]", get_application_model())
    production_application = application_model._default_manager.filter(
        user_id=OuterRef("pk"), is_sandbox=False
    )
    return (
        Q(is_staff=True)
        | Q(is_superuser=True)
        | Q(is_active=True) & (Q(Exists(own_permission)) | Q(Exists(group_permission)))
        | Q(Exists(production_application))
    )


def mfa_required(actor: object) -> bool:
    """True when ``actor`` must have a method before using the API.

    A saved Account that ``_requires_mfa`` matches, read from the database
    by id in one query.
    """
    pk = _saved_account_pk(actor)
    if pk is None:
        return False
    required: bool = get_user_model().objects.filter(_requires_mfa(), pk=pk).exists()
    return required


def lacks_required_mfa(actor: object) -> bool:
    """True when ``actor`` needs a method (``mfa_required``) and has none
    (``has_mfa``), in one query. The API uses ``mfa_status`` instead, since
    it also needs ``has_mfa`` alone for its session rule."""
    pk = _saved_account_pk(actor)
    if pk is None:
        return False
    lacks: bool = (
        get_user_model()
        .objects.filter(_requires_mfa(), pk=pk)
        .exclude(pk__in=accounts_with_mfa())
        .exists()
    )
    return lacks


@dataclass(frozen=True)
class MFAStatus:
    """Whether an Account must have a method, and whether it has one."""

    required: bool
    enrolled: bool


def mfa_status(actor: object) -> MFAStatus:
    """``mfa_required`` and ``has_mfa`` for ``actor``, in one query.

    Both False for an unsaved or anonymous actor.
    """
    pk = _saved_account_pk(actor)
    if pk is None:
        return MFAStatus(required=False, enrolled=False)
    row = (
        get_user_model()
        .objects.filter(pk=pk)
        .annotate(
            required=ExpressionWrapper(_requires_mfa(), output_field=BooleanField()),
            enrolled=Exists(
                Authenticator.objects.filter(
                    user_id=OuterRef("pk"), type__in=MFA_METHOD_TYPES
                )
            ),
        )
        .values_list("required", "enrolled")
        .first()
    )
    if row is None:
        return MFAStatus(required=False, enrolled=False)
    return MFAStatus(required=bool(row[0]), enrolled=bool(row[1]))


def session_passed_mfa(session: SessionBase, account: "_User") -> bool:
    """True when ``session`` holds a second factor record for ``account``.

    allauth records each authentication step of a session, bound to the
    Account that took it. A login that passed the MFA stage, or an MFA
    reauthentication, leaves a record with method ``mfa``.
    """
    records = get_authentication_records(SimpleNamespace(session=session), account)
    return any(record.get("method") == "mfa" for record in records)


def _latest_at(
    session: SessionBase, account: "_User", *, mfa_only: bool
) -> float | None:
    records = get_authentication_records(SimpleNamespace(session=session), account)
    times = [
        float(record["at"])
        for record in records
        if isinstance(record, dict)
        and (not mfa_only or record.get("method") == "mfa")
        and isinstance(record.get("at"), int | float)
        and not isinstance(record.get("at"), bool)
        and math.isfinite(record["at"])
    ]
    return max(times, default=None)


def last_mfa_at(session: SessionBase, account: "_User") -> float | None:
    """When ``account`` last passed the second factor in ``session``.

    The latest ``at`` (seconds since the epoch, allauth's ``time.time()``)
    of the session's ``mfa`` records bound to ``account``, or None when it
    has none. A record without a finite number for its time is skipped.
    The admin's time limits read it (``config/admin.py``), and so does the
    Organizer step-up (``config.auth.require_step_up``).
    """
    return _latest_at(session, account, mfa_only=True)


def last_authenticated_at(session: SessionBase, account: "_User") -> float | None:
    """When ``account`` last took any authentication step in ``session``.

    Like ``last_mfa_at``, but over every allauth record (a login or a
    reauthentication, by password or MFA). The step-up reads it for an
    Account without MFA (``config.auth.require_step_up``).
    """
    return _latest_at(session, account, mfa_only=False)


def record_mfa_change(account: "_User", change: MFAChange, kind: str) -> None:
    """Record one ``mfa.change`` Audit Event for ``account``.

    ``kind`` is the authenticator type (``totp``, ``recovery_codes``, or
    ``webauthn``).
    Never a secret, a code, or a seed. Call it inside the change's
    ``transaction.atomic()``.
    """
    record_event(
        action=AuditAction.MFA_CHANGE,
        actor=account,
        target=account,
        after={"change": change, "type": kind},
    )

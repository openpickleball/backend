"""allauth's headless MFA views, with OPEN's rules (issue #99, ADR 0043).

OPEN subclasses the four allauth views that use or change an Account's
authenticators and serves them at allauth's paths, for both clients
(``headless_urlpatterns()``).

Audit. Each MFA change records one ``mfa.change`` Audit Event in the
change's own transaction (``apps.accounts.mfa.record_mfa_change``), so
the change and its event commit together or not at all. allauth's own
signals for these changes run after commit, which is too late.

- ``ManageTOTPView``: adding TOTP records ``added totp``, and
  ``added recovery_codes`` when the first method made the codes.
  Removing it records ``removed totp``, and ``removed recovery_codes``
  when no other method keeps them.
- ``ManageRecoveryCodesView``: making new codes records
  ``regenerated recovery_codes``.
- ``AuthenticateView`` and ``ReauthenticateView``: a recovery code used
  to log in or to reauthenticate records ``used recovery_codes``.

Lock order. Every view's ``dispatch`` runs in ``transaction.atomic()``
and first locks the Account row ``FOR UPDATE`` (the signed-in Account,
or for the login stage the pending login's Account), before the input is
validated or any authenticator is read. allauth then takes the recovery
codes row ``FOR UPDATE`` when it checks a code, and a regeneration locks
the Account row again (already held). So one Account's MFA changes run in
turn, always Account row first: a second removal reads the authenticators
again under the lock and records nothing, and a recovery code login
cannot deadlock with a regeneration.

Sessions. For an Account that has MFA, the management views (TOTP add
and remove, recovery codes view and regenerate) need a session that
passed the second factor (``session_passed_mfa``: an MFA login, an MFA
reauthentication, or this enrollment). Any other session, such as the
admin login's password-only session, gets allauth's 401 listing only the
MFA reauthentication flow; a password reauthentication does not satisfy
it, a code reauthentication (``auth/2fa/reauthenticate``) does. Adding
TOTP records an ``mfa`` authentication record for the enrolling session
(no row id, not a reauthentication, so allauth's recent reauthentication
rule is unaffected), so that session keeps its access.

Order of checks, a deliberate departure from allauth: for TOTP add and
remove and for regenerating codes, the reauthentication requirement is
checked before allauth validates the input, so a code is never tested
for a session that may not act; such a session gets allauth's 401 with
its flows whatever the code.

Row ids. allauth records an MFA step with the Authenticator's row id and
returns the records in every authenticated response. OPEN's login stage
and reauthentication remove that id from the session's records before
answering (nothing in allauth reads it for TOTP or recovery codes).

A removal records its events first, in a savepoint, so a refusal raised
inside it rolls the events back before allauth answers. A refused or
wrong code writes nothing. Otherwise the responses, status codes, and
errors are allauth's own: each view calls allauth's flows and returns
allauth's response classes.
"""

from typing import TYPE_CHECKING, Any, ClassVar, cast

from allauth.account.authentication import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.internal.flows.login import record_authentication
from allauth.account.internal.flows.reauthentication import (
    raise_if_reauthentication_required,
)
from allauth.account.internal.stagekit import get_pending_stage
from allauth.headless.base.response import (
    AuthenticationResponse,
    ReauthenticationResponse,
)
from allauth.headless.mfa import response as mfa_response
from allauth.headless.mfa.views import AuthenticateView as BaseAuthenticateView
from allauth.headless.mfa.views import (
    ManageRecoveryCodesView as BaseManageRecoveryCodesView,
)
from allauth.headless.mfa.views import ManageTOTPView as BaseManageTOTPView
from allauth.headless.mfa.views import ReauthenticateView as BaseReauthenticateView
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal import flows as totp_flows
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.decorators import method_decorator

from apps.accounts.mfa import has_mfa, record_mfa_change, session_passed_mfa

if TYPE_CHECKING:
    from django.contrib.auth.models import _User
    from django.http import HttpRequest, HttpResponse, HttpResponseBase

# allauth's flow id for a reauthentication with an MFA code.
_MFA_REAUTHENTICATE_FLOW = "mfa_reauthenticate"


def _account(user: object) -> "_User":
    """The Account itself, not Django's lazy ``request.user``."""
    return cast("_User", getattr(user, "_wrapped", user))


def _lock_account(pk: int) -> None:
    """Take the Account row ``FOR UPDATE`` (the first lock of every view)."""
    list(
        get_user_model()
        .objects.select_for_update()
        .filter(pk=pk)
        .values_list("pk", flat=True)
    )


def _strip_mfa_ids(request: "HttpRequest") -> None:
    """Remove the Authenticator row id from the session's MFA records."""
    records = request.session.get(AUTHENTICATION_METHODS_SESSION_KEY)
    if not isinstance(records, list):
        return
    stripped = [
        {key: value for key, value in record.items() if key != "id"}
        if isinstance(record, dict) and record.get("method") == "mfa"
        else record
        for record in records
    ]
    if stripped != records:
        request.session[AUTHENTICATION_METHODS_SESSION_KEY] = stripped


def _record_if_recovery_code(authenticator: Authenticator) -> None:
    if authenticator.type == Authenticator.Type.RECOVERY_CODES:
        record_mfa_change(
            _account(authenticator.user), "used", Authenticator.Type.RECOVERY_CODES
        )


class MFAReauthenticationResponse(ReauthenticationResponse):
    """allauth's reauthentication 401, offering only the MFA code flow."""

    def _get_flows(self, request: "HttpRequest", user: Any) -> list[Any]:
        flows: list[Any] = super()._get_flows(request, user)
        return [flow for flow in flows if flow.get("id") == _MFA_REAUTHENTICATE_FLOW]


class _LocksSignedInAccount:
    """Lock the signed-in Account's row first, inside the atomic dispatch."""

    def dispatch(
        self, request: "HttpRequest", *args: Any, **kwargs: Any
    ) -> "HttpResponseBase":
        if request.user.is_authenticated:
            _lock_account(request.user.pk)
        response: HttpResponseBase = super().dispatch(  # type: ignore[misc]
            request, *args, **kwargs
        )
        return response


class _GuardsMFAChanges(_LocksSignedInAccount):
    """The session and reauthentication checks of the management views.

    ``session_checked`` methods need, for an Account with MFA, a session
    that passed MFA. ``reauthentication_first`` methods also need allauth's
    recent reauthentication before the input is validated.
    """

    session_checked: ClassVar[frozenset[str]] = frozenset()
    reauthentication_first: ClassVar[frozenset[str]] = frozenset()

    def handle(
        self, request: "HttpRequest", *args: Any, **kwargs: Any
    ) -> "HttpResponseBase":
        method = request.method or ""
        if request.user.is_authenticated:
            account = _account(request.user)
            if (
                method in self.session_checked
                and has_mfa(account)
                and not session_passed_mfa(request.session, account)
            ):
                return MFAReauthenticationResponse(request)
            if method in self.reauthentication_first:
                raise_if_reauthentication_required(request, app="allauth.mfa")
        response: HttpResponseBase = super().handle(  # type: ignore[misc]
            request, *args, **kwargs
        )
        return response


@method_decorator(transaction.atomic, name="dispatch")
class AuthenticateView(BaseAuthenticateView):
    """allauth's MFA login stage, recording a recovery code's use."""

    def dispatch(
        self, request: "HttpRequest", *args: Any, **kwargs: Any
    ) -> "HttpResponseBase":
        stage = get_pending_stage(request)
        if stage is not None and stage.login.user is not None:
            _lock_account(stage.login.user.pk)
        response: HttpResponseBase = super().dispatch(request, *args, **kwargs)
        return response

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        # allauth's own steps, with the row id removed before the answer.
        self.input.save()
        _strip_mfa_ids(request)
        _record_if_recovery_code(self.input.authenticator)
        return cast("HttpResponse", self.respond_next_stage())


@method_decorator(transaction.atomic, name="dispatch")
class ReauthenticateView(_LocksSignedInAccount, BaseReauthenticateView):
    """allauth's MFA reauthentication, recording a recovery code's use."""

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        # allauth's own steps, with the row id removed before the answer.
        self.input.save()
        _strip_mfa_ids(request)
        _record_if_recovery_code(self.input.authenticator)
        return cast("HttpResponse", AuthenticationResponse(request))


@method_decorator(transaction.atomic, name="dispatch")
class ManageTOTPView(_GuardsMFAChanges, BaseManageTOTPView):
    """allauth's TOTP view, recording each authenticator added or removed."""

    session_checked = frozenset({"POST", "DELETE"})
    reauthentication_first = frozenset({"POST", "DELETE"})

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        # allauth's own steps, since activate_totp's second result (the
        # recovery codes it made, if any) decides the second event.
        totp, recovery_codes = totp_flows.activate_totp(request, self.input)
        account = _account(request.user)
        record_mfa_change(account, "added", Authenticator.Type.TOTP)
        if recovery_codes is not None:
            record_mfa_change(account, "added", Authenticator.Type.RECOVERY_CODES)
        # The enrolling session has passed the second factor it just set up.
        record_authentication(request, account, "mfa", type=Authenticator.Type.TOTP)
        return cast("HttpResponse", mfa_response.TOTPResponse(request, totp))

    def delete(
        self, request: "HttpRequest", *args: Any, **kwargs: Any
    ) -> "HttpResponse":
        # Read under the Account lock, so a second removal finds nothing.
        totp = self._get_authenticator()
        if totp is None:
            nothing: HttpResponse = super().delete(request, *args, **kwargs)
            return nothing
        account = _account(request.user)
        # The codes go with the last method (allauth's
        # delete_dangling_recovery_codes).
        codes_go = (
            not Authenticator.objects.filter(user_id=account.pk)
            .exclude(type=Authenticator.Type.RECOVERY_CODES)
            .exclude(pk=totp.pk)
            .exists()
            and Authenticator.objects.filter(
                user_id=account.pk, type=Authenticator.Type.RECOVERY_CODES
            ).exists()
        )
        # A savepoint: a refusal raised inside it rolls the events back
        # before allauth turns it into its 401.
        with transaction.atomic():
            record_mfa_change(account, "removed", Authenticator.Type.TOTP)
            if codes_go:
                record_mfa_change(account, "removed", Authenticator.Type.RECOVERY_CODES)
            removed: HttpResponse = super().delete(request, *args, **kwargs)
            return removed


@method_decorator(transaction.atomic, name="dispatch")
class ManageRecoveryCodesView(_GuardsMFAChanges, BaseManageRecoveryCodesView):
    """allauth's recovery codes view, recording each regeneration."""

    session_checked = frozenset({"GET", "POST"})
    reauthentication_first = frozenset({"POST"})

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        response: HttpResponse = super().post(request, *args, **kwargs)
        record_mfa_change(
            _account(request.user), "regenerated", Authenticator.Type.RECOVERY_CODES
        )
        return response

"""allauth's headless MFA views, with OPEN's Audit Events (issue #99, ADR 0043).

Each MFA change records one ``mfa.change`` Audit Event in the change's
own transaction (``apps.accounts.mfa.record_mfa_change``), so the change
and its event commit together or not at all. allauth's own signals for
these changes run after commit, which is too late, so OPEN subclasses
the four views that change an Account's authenticators and serves them
at allauth's paths, for both clients (``headless_urlpatterns()``):

- ``ManageTOTPView``: adding TOTP records ``added totp``, and
  ``added recovery_codes`` when the first method made the codes.
  Removing it records ``removed totp``, and ``removed recovery_codes``
  when no other method keeps them.
- ``ManageRecoveryCodesView``: making new codes records
  ``regenerated recovery_codes``.
- ``AuthenticateView`` and ``ReauthenticateView``: a recovery code used
  to log in or to reauthenticate records ``used recovery_codes``.

Every view's ``dispatch`` runs in ``transaction.atomic()``. allauth turns
a missing reauthentication (``ReauthenticationRequired``) into its 401
inside ``dispatch``, so the removal, whose events come first, runs in its
own savepoint: the refusal rolls the events back before allauth answers.
A recovery code is marked used while the input is validated (allauth
locks its row ``FOR UPDATE``), inside the same transaction as the event.
A refused or wrong code writes nothing. The responses, status codes, and
errors are allauth's own: each view calls allauth's flows and returns
allauth's response classes.
"""

from typing import TYPE_CHECKING, Any, cast

from allauth.headless.mfa import response as mfa_response
from allauth.headless.mfa.views import AuthenticateView as BaseAuthenticateView
from allauth.headless.mfa.views import (
    ManageRecoveryCodesView as BaseManageRecoveryCodesView,
)
from allauth.headless.mfa.views import ManageTOTPView as BaseManageTOTPView
from allauth.headless.mfa.views import ReauthenticateView as BaseReauthenticateView
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal import flows as totp_flows
from django.db import transaction
from django.utils.decorators import method_decorator

from apps.accounts.mfa import record_mfa_change

if TYPE_CHECKING:
    from django.contrib.auth.models import _User
    from django.http import HttpRequest, HttpResponse


def _account(user: object) -> "_User":
    """The Account itself, not Django's lazy ``request.user``."""
    return cast("_User", getattr(user, "_wrapped", user))


def _record_if_recovery_code(authenticator: Authenticator) -> None:
    if authenticator.type == Authenticator.Type.RECOVERY_CODES:
        record_mfa_change(
            _account(authenticator.user), "used", Authenticator.Type.RECOVERY_CODES
        )


@method_decorator(transaction.atomic, name="dispatch")
class AuthenticateView(BaseAuthenticateView):
    """allauth's MFA login stage, recording a recovery code's use."""

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        response: HttpResponse = super().post(request, *args, **kwargs)
        _record_if_recovery_code(self.input.authenticator)
        return response


@method_decorator(transaction.atomic, name="dispatch")
class ReauthenticateView(BaseReauthenticateView):
    """allauth's MFA reauthentication, recording a recovery code's use."""

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        response: HttpResponse = super().post(request, *args, **kwargs)
        _record_if_recovery_code(self.input.authenticator)
        return response


@method_decorator(transaction.atomic, name="dispatch")
class ManageTOTPView(BaseManageTOTPView):
    """allauth's TOTP view, recording each authenticator added or removed."""

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        # allauth's own steps, since activate_totp's second result (the
        # recovery codes it made, if any) decides the second event.
        totp, recovery_codes = totp_flows.activate_totp(request, self.input)
        account = _account(request.user)
        record_mfa_change(account, "added", Authenticator.Type.TOTP)
        if recovery_codes is not None:
            record_mfa_change(account, "added", Authenticator.Type.RECOVERY_CODES)
        return cast("HttpResponse", mfa_response.TOTPResponse(request, totp))

    def delete(
        self, request: "HttpRequest", *args: Any, **kwargs: Any
    ) -> "HttpResponse":
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
        # A savepoint: a refused reauthentication raises inside it, so the
        # events roll back before allauth turns the refusal into its 401.
        with transaction.atomic():
            record_mfa_change(account, "removed", Authenticator.Type.TOTP)
            if codes_go:
                record_mfa_change(account, "removed", Authenticator.Type.RECOVERY_CODES)
            removed: HttpResponse = super().delete(request, *args, **kwargs)
            return removed


@method_decorator(transaction.atomic, name="dispatch")
class ManageRecoveryCodesView(BaseManageRecoveryCodesView):
    """allauth's recovery codes view, recording each regeneration."""

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        response: HttpResponse = super().post(request, *args, **kwargs)
        record_mfa_change(
            _account(request.user), "regenerated", Authenticator.Type.RECOVERY_CODES
        )
        return response

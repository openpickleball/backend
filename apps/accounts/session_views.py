"""allauth's headless views for sessions and passwords, with OPEN's rules
(#100, ADR 0047).

``headless_urlpatterns()`` serves these at allauth's paths, for both
clients, ahead of allauth's URLconf.

- ``SessionsView`` (``auth/sessions``): lists the player's sessions with
  an opaque id (``public_session_id``), never the row id, and ends the
  sessions the player names by those ids, recording one ``session.end``
  Audit Event per session in the transaction that ends them.
- ``ChangePasswordView`` (``account/password/change``): allauth's change
  or first set, then one ``password.change`` Audit Event (``method``
  ``change`` or ``set``) and the end of the Account's
  other sessions and app tokens, all in one transaction. The session
  that made the change stays signed in.
- ``ResetPasswordView`` (``auth/password/reset``): allauth's reset from a
  key, then one ``password.change`` Audit Event and the end of every
  session of the Account, in one transaction. It answers after the
  sessions ended, so a session signed in to the Account is told it is
  signed out.

A refused input changes nothing and records nothing: allauth answers it
before ``post`` runs.
"""

from typing import TYPE_CHECKING, Any, ClassVar, cast

from allauth.account.internal.flows.logout import logout
from allauth.headless.account.views import (
    ChangePasswordView as BaseChangePasswordView,
)
from allauth.headless.account.views import ResetPasswordView as BaseResetPasswordView
from allauth.headless.base.response import APIResponse, AuthenticationResponse
from allauth.headless.internal.restkit import inputs
from allauth.headless.usersessions.views import SessionsView as BaseSessionsView
from allauth.usersessions.models import UserSession
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounts.sessions import (
    end_all_sessions,
    end_other_sessions,
    end_sessions_by_player,
    public_session_id,
    sessions_by_public_id,
)
from apps.audit.models import AuditAction
from apps.audit.services import record_event

if TYPE_CHECKING:
    from django.contrib.auth.models import _User
    from django.http import HttpRequest, HttpResponse, HttpResponseBase

# Fixed text: never the id that was sent.
_UNKNOWN_SESSION = "Select sessions from your session list."


def _account(user: object) -> "_User":
    """The Account itself, not Django's lazy ``request.user``."""
    return cast("_User", getattr(user, "_wrapped", user))


class SelectSessionsInput(inputs.Input):
    """The sessions to end: a list of ids from the player's session list.

    An id that is not one of the Account's sessions, another Account's
    included, refuses the whole request with one fixed message.
    """

    def __init__(self, *args: Any, user: "_User", **kwargs: Any) -> None:
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        sent = self.data.get("sessions")
        if (
            not isinstance(sent, list)
            or not sent
            or not all(isinstance(value, str) for value in sent)
        ):
            raise ValidationError(_UNKNOWN_SESSION, code="invalid")
        known = sessions_by_public_id(self.user)
        if any(value not in known for value in sent):
            raise ValidationError(_UNKNOWN_SESSION, code="invalid")
        cleaned["sessions"] = [known[value] for value in dict.fromkeys(sent)]
        return cleaned


class SessionsResponse(APIResponse):
    """allauth's session list, each session by its public id."""

    def __init__(self, request: "HttpRequest", sessions: list[UserSession]) -> None:
        super().__init__(
            request,
            data=[
                {
                    "user_agent": row.user_agent,
                    "ip": row.ip,
                    "created_at": row.created_at.timestamp(),
                    "is_current": row.is_current(),
                    "id": public_session_id(row),
                }
                for row in sessions
            ],
        )


class SessionsView(BaseSessionsView):
    """allauth's session list and end, by public ids, audited."""

    input_class: ClassVar[dict[str, type[inputs.Input]]] = {
        "DELETE": SelectSessionsInput
    }

    def delete(
        self, request: "HttpRequest", *args: Any, **kwargs: Any
    ) -> "HttpResponseBase":
        account = _account(request.user)
        with transaction.atomic():
            end_sessions_by_player(
                request, account, self.input.cleaned_data["sessions"]
            )
        if request.user.is_authenticated:
            return self._respond_session_list()
        ended: HttpResponseBase = AuthenticationResponse(request)
        return ended

    def _respond_session_list(self) -> SessionsResponse:
        account = _account(self.request.user)
        return SessionsResponse(
            self.request, UserSession.objects.purge_and_list(account)
        )


class ChangePasswordView(BaseChangePasswordView):
    """allauth's password change, audited, ending the other sessions."""

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        account = _account(request.user)
        with transaction.atomic():
            # No current password is allauth's set path (a first password).
            method = (
                "change" if self.input.cleaned_data.get("current_password") else "set"
            )
            response: HttpResponse = super().post(request, *args, **kwargs)
            record_event(
                action=AuditAction.PASSWORD_CHANGE,
                actor=account,
                target=account,
                after={"method": method},
            )
            end_other_sessions(request, account)
        return response


class ResetPasswordView(BaseResetPasswordView):
    """allauth's password reset from a key, audited, ending every session."""

    def post(self, request: "HttpRequest", *args: Any, **kwargs: Any) -> "HttpResponse":
        account = cast("_User", self.input.user)
        with transaction.atomic():
            super().post(request, *args, **kwargs)
            record_event(
                action=AuditAction.PASSWORD_CHANGE,
                actor=account,
                target=account,
                after={"method": "reset"},
            )
            end_all_sessions(request, account)
            if getattr(request.user, "pk", None) == account.pk:
                # Signed in to this Account without a user session row:
                # that session ends too.
                logout(request)
        # Built after the sessions ended, so a reset from a session signed
        # in to this Account answers signed out.
        answer: HttpResponse = AuthenticationResponse(request)
        return answer

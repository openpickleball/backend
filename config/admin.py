"""OPEN's admin site: staff with MFA, a recent code, and audited changes.

Staff can read and change other players' records, verify Organizers, and
void matches, so the admin asks more than Django's (issue #99, #101, ADR
0043):

- It is served at ``settings.ADMIN_URL``, a private path in production
  (``config/urls.py``).
- It opens only to an active staff Account that has MFA
  (``apps.accounts.mfa.has_mfa``, read from the database on every
  request) whose session passed MFA within ``ADMIN_SESSION_SECONDS``
  (``apps.accounts.mfa.last_mfa_at``).
- A signed-in Account that is not active staff gets 404 on every admin
  URL, the login page included, the same answer as an unknown path.
- Every unsafe request (anything but GET, HEAD, OPTIONS), logout aside,
  needs an MFA step within ``ADMIN_REAUTHENTICATION_SECONDS``. Otherwise
  nothing runs and it is redirected to the login page, which then asks
  for a code. A redirected form's data is lost: the admin enters it
  again.
- Every add, change, delete, and custom action made through it records
  an Audit Event (``register`` adds ``apps.audit.admin_audit``'s mixin
  to every admin).
- A staff Account's own password change records ``password.change``.
- The login page takes no password. Its script
  (``apps/accounts/static/accounts/admin_login.js``) signs in through
  allauth's headless browser client (``auth/login``, then
  ``auth/2fa/authenticate``), so allauth's login rules, rate limits, and
  OPEN's MFA views apply, or confirms a code for a signed-in session
  (``auth/2fa/reauthenticate``). A staff Account without MFA gets a page
  that tells it to set one up in the OPEN app.

``config.apps.OpenAdminConfig`` installs this site as ``admin.site``, so
every ``@admin.register`` stays as it is.
"""

import time
from collections.abc import Callable
from functools import update_wrapper
from typing import TYPE_CHECKING, Any, TypeVar, cast

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
)
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from apps.accounts.mfa import has_mfa, last_mfa_at
from apps.audit.admin_audit import audited_admin_class
from apps.audit.models import AuditAction
from apps.audit.services import record_event

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

MFA_REQUIRED_MESSAGE = (
    "Set up two-factor authentication in the OPEN app before using the admin."
)

# allauth's headless browser client, which the login page's script calls.
ALLAUTH_BROWSER_BASE = "/_allauth/browser/v1"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_View = TypeVar("_View", bound=Callable[..., HttpResponseBase])


def _account(request: HttpRequest) -> "_User | None":
    """The signed-in Account, or None."""
    # Reading is_authenticated first sets up Django's lazy request.user.
    if not request.user.is_authenticated:
        return None
    return cast("_User", getattr(request.user, "_wrapped", request.user))


def _is_active_staff(account: "_User") -> bool:
    return bool(account.is_active and account.is_staff)


def _mfa_within(request: HttpRequest, account: "_User", seconds: int) -> bool:
    """True when the session's latest MFA step is at most ``seconds`` old."""
    last = last_mfa_at(request.session, account)
    return last is not None and time.time() - last <= seconds


class OpenAdminSite(admin.AdminSite):
    """Django's admin site, open only to active staff with a recent code."""

    def register(
        self,
        model_or_iterable: Any,
        admin_class: type[admin.ModelAdmin[Any]] | None = None,
        **options: Any,
    ) -> None:
        """Register with ``AdminAuditMixin`` first in the admin's bases
        (``apps/audit/admin_audit.py``), so every admin change is audited.
        """
        super().register(
            model_or_iterable,
            audited_admin_class(admin_class or admin.ModelAdmin),
            **options,
        )

    def has_permission(self, request: HttpRequest) -> bool:
        account = _account(request)
        return (
            account is not None
            and _is_active_staff(account)
            and has_mfa(account)
            and _mfa_within(request, account, settings.ADMIN_SESSION_SECONDS)
        )

    def admin_view(self, view: _View, cacheable: bool = False) -> _View:
        """Django's wrapper, behind the 404 rule and the write rule."""
        inner = super().admin_view(view, cacheable)

        def guarded(
            request: HttpRequest, *args: Any, **kwargs: Any
        ) -> HttpResponseBase:
            self._refuse_non_staff(request)
            if (
                request.method not in SAFE_METHODS
                and request.path != reverse("admin:logout", current_app=self.name)
                and self.has_permission(request)
                and not self._recent_code(request)
            ):
                return redirect_to_login(
                    request.path, reverse("admin:login", current_app=self.name)
                )
            return inner(request, *args, **kwargs)

        return cast(_View, update_wrapper(never_cache(guarded), inner))

    def password_change(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> TemplateResponse:
        """Django's own password change, recording ``password.change`` in
        the change's transaction when it succeeds (a redirect), and nothing
        when the form is refused."""
        with transaction.atomic():
            response = super().password_change(request, extra_context)
            account = _account(request)
            if (
                request.method == "POST"
                and response.status_code == 302
                and account is not None
            ):
                record_event(
                    action=AuditAction.PASSWORD_CHANGE, actor=account, target=account
                )
            return response

    @staticmethod
    def _refuse_non_staff(request: HttpRequest) -> None:
        """404 for a signed-in Account that is not active staff."""
        account = _account(request)
        if account is not None and not _is_active_staff(account):
            raise Http404

    @staticmethod
    def _recent_code(request: HttpRequest) -> bool:
        account = _account(request)
        return account is not None and _mfa_within(
            request, account, settings.ADMIN_REAUTHENTICATION_SECONDS
        )

    def _next(self, request: HttpRequest) -> str:
        """The page to open after signing in: ``next`` when it stays on
        this site, otherwise the admin index."""
        target = request.GET.get(REDIRECT_FIELD_NAME, "")
        if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=True
        ):
            return target
        return reverse("admin:index", current_app=self.name)

    # csrf_exempt: the page accepts no POST at all, so a POST gets 405
    # whatever its token. Its form posts nowhere: the script calls
    # allauth's endpoints, which keep their own CSRF check.
    @method_decorator(csrf_exempt)
    @method_decorator(never_cache)
    @login_not_required
    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        """OPEN's login page: a sign-in or a code, never a password form."""
        self._refuse_non_staff(request)
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])
        request.current_app = self.name
        account = _account(request)
        context = {**self.each_context(request), "title": "Log in", "subtitle": None}
        if account is not None and not has_mfa(account):
            return TemplateResponse(
                request,
                "admin/open_mfa_required.html",
                {**context, "message": MFA_REQUIRED_MESSAGE},
                status=403,
            )
        target = self._next(request)
        if (
            account is not None
            and self.has_permission(request)
            and self._recent_code(request)
        ):
            return HttpResponseRedirect(target)
        context.update(
            {
                "mode": "login" if account is None else "code",
                "next": target,
                "allauth_base": ALLAUTH_BROWSER_BASE,
                "csrf_cookie_name": settings.CSRF_COOKIE_NAME,
                **(extra_context or {}),
            }
        )
        return TemplateResponse(request, "admin/open_login.html", context)

"""OPEN's admin site: every staff Account needs MFA (issue #99, ADR 0043).

Staff can read and change other players' records, verify Organizers, and
void matches, so the admin needs a second factor on the Account
(``apps.accounts.mfa.has_mfa``), read from the database on every request.
A signed-in staff Account without a method gets the admin login page on
every admin URL, as any Account without admin permission does, and the
login form refuses it with fixed text that names no Account. A method is
set up through allauth's headless API in the OPEN app.

The admin login itself asks for the password only: asking for the code
there is #101 (``apps.accounts.mfa.session_passed_mfa`` is the check).

``config.apps.OpenAdminConfig`` installs this site as ``admin.site``, so
every ``@admin.register`` stays as it is.
"""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError
from django.http import HttpRequest

from apps.accounts.mfa import has_mfa

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

MFA_REQUIRED_MESSAGE = (
    "Set up two-factor authentication in the OPEN app before using the admin."
)


class OpenAdminAuthenticationForm(AdminAuthenticationForm):
    """Django's admin login form, refusing a staff Account without MFA."""

    def confirm_login_allowed(self, user: "_User") -> None:
        super().confirm_login_allowed(user)
        if not has_mfa(user):
            raise ValidationError(MFA_REQUIRED_MESSAGE, code="mfa_required")


class OpenAdminSite(admin.AdminSite):
    """Django's admin site, open only to active staff with MFA."""

    login_form = OpenAdminAuthenticationForm

    def has_permission(self, request: HttpRequest) -> bool:
        return super().has_permission(request) and has_mfa(
            getattr(request.user, "_wrapped", request.user)
        )

from typing import Any

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

from apps.accounts.forms import UserChangeForm, UserCreationForm
from apps.accounts.models import User
from apps.audit.admin_access import AccessAuditAdminMixin

# allauth registers an admin for EmailAddress, whose __str__ returns the
# address, so the admin log's object_repr would hold an identifying field
# (ADR 0009). allauth.account comes before apps.accounts in INSTALLED_APPS,
# so its admin module has run by now. The address shows on the Account.
admin.site.unregister(EmailAddress)
# allauth.mfa registers an admin for Authenticator, which would show TOTP
# secrets and recovery code seeds to staff (ADR 0009: never shown, ADR
# 0043). allauth.mfa also comes before apps.accounts.
admin.site.unregister(Authenticator)
# Django's Group admin, replaced below: only a superuser changes groups.
# django.contrib.auth also comes before apps.accounts.
admin.site.unregister(Group)

# The fields that grant admin and API power (#101). Only a superuser
# changes them; anyone else sees them read-only.
GRANT_FIELDS = ("is_staff", "is_superuser", "groups", "user_permissions")


def _is_superuser(request: HttpRequest) -> bool:
    return bool(request.user.is_active and request.user.is_superuser)


def _holds_power(account: Any) -> bool:
    """True for a staff or superuser Account."""
    return bool(account.is_staff or account.is_superuser)


@admin.register(User)
class UserAdmin(AccessAuditAdminMixin, BaseUserAdmin[User]):
    form = UserChangeForm
    add_form = UserCreationForm
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "usable_password", "password1", "password2"),
            },
        ),
    )
    list_display = ("email", "is_staff", "is_active")
    # Set by Django, never by staff. Their split date-time fields also drop
    # microseconds, so a save would rewrite them (#101).
    readonly_fields = ("last_login", "date_joined")
    search_fields = ("email",)
    ordering = ("email",)

    def get_readonly_fields(
        self, request: HttpRequest, obj: User | None = None
    ) -> tuple[str, ...]:
        fields = tuple(super().get_readonly_fields(request, obj))
        if _is_superuser(request):
            return fields
        return fields + tuple(f for f in GRANT_FIELDS if f not in fields)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Only a superuser changes a staff or superuser Account (#101)."""
        if obj is not None and _holds_power(obj) and not _is_superuser(request):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Only a superuser deletes a staff or superuser Account (#101)."""
        if obj is not None and _holds_power(obj) and not _is_superuser(request):
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        """Delete the selection, or nothing when it holds a staff or
        superuser Account and the actor is not a superuser.

        ``delete_selected`` already refuses such a selection on its
        confirmation page; this checks again at the delete itself.
        """
        if not _is_superuser(request) and (
            queryset.filter(is_staff=True).exists()
            or queryset.filter(is_superuser=True).exists()
        ):
            raise PermissionDenied
        super().delete_queryset(request, queryset)

    def user_change_password(
        self, request: HttpRequest, id: str, form_url: str = ""
    ) -> HttpResponse:
        """Django's set-password view, in one transaction, so the new
        password and its Audit Event commit together (#101)."""
        with transaction.atomic():
            response: HttpResponse = super().user_change_password(request, id, form_url)
            return response


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin):
    """Django's Group admin: only a superuser adds, changes, or deletes a
    Group, since a Group grants permissions (#101). Staff with the view
    permission can still read them."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return _is_superuser(request) and super().has_add_permission(request)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return _is_superuser(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return _is_superuser(request) and super().has_delete_permission(request, obj)

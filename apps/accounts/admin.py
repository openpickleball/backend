from allauth.account.models import EmailAddress
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.accounts.forms import UserChangeForm, UserCreationForm
from apps.accounts.models import User
from apps.audit.admin_access import AccessAuditAdminMixin

# allauth registers an admin for EmailAddress, whose __str__ returns the
# address, so the admin log's object_repr would hold an identifying field
# (ADR 0009). allauth.account comes before apps.accounts in INSTALLED_APPS,
# so its admin module has run by now. The address shows on the Account.
admin.site.unregister(EmailAddress)


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
    search_fields = ("email",)
    ordering = ("email",)

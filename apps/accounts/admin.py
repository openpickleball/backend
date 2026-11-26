from typing import Any

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django_celery_beat.admin import (
    ClockedScheduleAdmin as BaseClockedScheduleAdmin,
)
from django_celery_beat.admin import (
    CrontabScheduleAdmin as BaseCrontabScheduleAdmin,
)
from django_celery_beat.admin import (
    IntervalScheduleAdmin as BaseIntervalScheduleAdmin,
)
from django_celery_beat.admin import PeriodicTaskAdmin as BasePeriodicTaskAdmin
from django_celery_beat.admin import SolarScheduleAdmin as BaseSolarScheduleAdmin
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)

from apps.accounts.forms import UserChangeForm, UserCreationForm
from apps.accounts.mfa import has_mfa
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
# django_celery_beat's admins, replaced below: only a superuser writes
# tasks or schedules. django_celery_beat also comes before apps.accounts.
admin.site.unregister(
    [PeriodicTask, IntervalSchedule, CrontabSchedule, SolarSchedule, ClockedSchedule]
)

# The fields that grant admin and API power (#101). Only a superuser
# changes them; anyone else sees them read-only.
GRANT_FIELDS = ("is_staff", "is_superuser", "groups", "user_permissions")

GRANT_NEEDS_MFA_MESSAGE = (
    "Staff and superuser status need two-factor authentication: the Account "
    "must set it up in the OPEN app first."
)


def _is_superuser(request: HttpRequest) -> bool:
    return bool(request.user.is_active and request.user.is_superuser)


def _holds_power(account: Any) -> bool:
    """True for an Account with any power beyond a player's: staff or
    superuser status, a group, or a permission of its own. The groups and
    permissions are read from the database."""
    return bool(
        account.is_staff
        or account.is_superuser
        or (
            account.pk is not None
            and (account.groups.exists() or account.user_permissions.exists())
        )
    )


class AccountChangeForm(UserChangeForm):
    """The Account change form, refusing a new grant of staff or superuser
    status to an Account without MFA (#101), with fixed text that names no
    Account. A grant the Account already holds is not checked again."""

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        for name in ("is_staff", "is_superuser"):
            # Before _post_clean, the instance still holds the stored values.
            newly_granted = cleaned.get(name) and not getattr(self.instance, name)
            if newly_granted and not has_mfa(self.instance):
                self.add_error(name, GRANT_NEEDS_MFA_MESSAGE)
        return cleaned


@admin.register(User)
class UserAdmin(AccessAuditAdminMixin, BaseUserAdmin[User]):
    form = AccountChangeForm
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
        """Only a superuser changes an Account that holds power (#101)."""
        if obj is not None and _holds_power(obj) and not _is_superuser(request):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Only a superuser deletes an Account that holds power (#101)."""
        if obj is not None and _holds_power(obj) and not _is_superuser(request):
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        """Delete the selection, or nothing when it holds an Account with
        power (``_holds_power``) and the actor is not a superuser.

        ``delete_selected`` already refuses such a selection on its
        confirmation page; this checks again at the delete itself.
        """
        if (
            not _is_superuser(request)
            and queryset.filter(
                Q(is_staff=True)
                | Q(is_superuser=True)
                | Q(groups__isnull=False)
                | Q(user_permissions__isnull=False)
            ).exists()
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


class _SuperuserWrites(admin.ModelAdmin[Any]):
    """Add, change, and delete for superusers only; viewing stays with
    Django's permissions."""

    def has_add_permission(self, request: HttpRequest, *args: Any) -> bool:
        return _is_superuser(request) and super().has_add_permission(request, *args)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return _is_superuser(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return _is_superuser(request) and super().has_delete_permission(request, obj)

    def has_superuser_permission(self, request: HttpRequest) -> bool:
        """Django offers an action marked ``permissions=["superuser"]`` only
        when this is True."""
        return _is_superuser(request)


def _superuser_action(name: str) -> Any:
    """django_celery_beat's PeriodicTask action ``name``, offered to and
    allowed for superusers only."""
    base = getattr(BasePeriodicTaskAdmin, name)

    def action(
        self: BasePeriodicTaskAdmin,
        request: HttpRequest,
        queryset: QuerySet[PeriodicTask],
    ) -> Any:
        if not _is_superuser(request):
            raise PermissionDenied
        return base(self, request, queryset)

    action.__name__ = name
    return admin.action(permissions=["superuser"], description=base.short_description)(
        action
    )


@admin.register(PeriodicTask)
class PeriodicTaskAdmin(_SuperuserWrites, BasePeriodicTaskAdmin):  # type: ignore[misc]
    """django_celery_beat's admin, for superusers only to write (#101).

    DatabaseScheduler sends whatever this table says, so a task added or
    changed here can send any registered task with chosen arguments, as
    often as every second. Adding, changing, deleting, and every action
    (enable, disable, toggle, and run, which sends tasks at once) are
    superuser-only. Viewing stays with the periodic task permissions."""

    enable_tasks = _superuser_action("enable_tasks")
    disable_tasks = _superuser_action("disable_tasks")
    toggle_tasks = _superuser_action("toggle_tasks")
    run_tasks = _superuser_action("run_tasks")


@admin.register(IntervalSchedule)
class IntervalScheduleAdmin(_SuperuserWrites, BaseIntervalScheduleAdmin):  # type: ignore[misc]
    """Superuser-only writes: a schedule decides when its tasks run (#101)."""


@admin.register(CrontabSchedule)
class CrontabScheduleAdmin(_SuperuserWrites, BaseCrontabScheduleAdmin):  # type: ignore[misc]
    """Superuser-only writes: a schedule decides when its tasks run (#101)."""


@admin.register(SolarSchedule)
class SolarScheduleAdmin(_SuperuserWrites, BaseSolarScheduleAdmin):  # type: ignore[misc]
    """Superuser-only writes: a schedule decides when its tasks run (#101)."""


@admin.register(ClockedSchedule)
class ClockedScheduleAdmin(_SuperuserWrites, BaseClockedScheduleAdmin):  # type: ignore[misc]
    """Superuser-only writes: a schedule decides when its tasks run (#101)."""

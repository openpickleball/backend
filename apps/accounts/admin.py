from contextlib import suppress
from typing import Any

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from django import forms
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
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
from django_celery_beat.admin import PeriodicTaskForm as BasePeriodicTaskForm
from django_celery_beat.admin import SolarScheduleAdmin as BaseSolarScheduleAdmin
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)

from apps.accounts.deletion import (
    PLACEHOLDER_EMAIL_MESSAGE,
    AccountAlreadyDeleted,
    delete_account,
    is_deleted_email,
)
from apps.accounts.forms import (
    AdminPasswordChangeForm,
    UserChangeForm,
    UserCreationForm,
)
from apps.accounts.mfa import has_totp
from apps.accounts.models import User
from apps.accounts.sessions import end_sessions_after_password_change
from apps.accounts.validators import normalize_email_address
from apps.audit.admin_access import AccessAuditAdminMixin
from config.admin_autocomplete import CHOICE_ERRORS

# allauth registers an admin for EmailAddress, whose __str__ returns the
# address, so the admin log's object_repr would hold an identifying field
# (ADR 0009). allauth.account comes before apps.accounts in INSTALLED_APPS,
# so its admin module has run by now. The address shows on the Account.
admin.site.unregister(EmailAddress)
# allauth.mfa registers an admin for Authenticator, which would show TOTP
# secrets and recovery code seeds to staff (ADR 0009: never shown, ADR
# 0043). allauth.mfa also comes before apps.accounts.
admin.site.unregister(Authenticator)
# allauth.usersessions registers an admin for UserSession, whose __str__
# and list show a session's IP address and user agent (ADR 0047). The
# player sees them in the session list; staff have no need to.
# allauth.usersessions also comes before apps.accounts.
admin.site.unregister(UserSession)
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
    "Staff and superuser status need two-factor authentication with an "
    "authenticator app: the Account must set it up in the OPEN app first."
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


def _is_cleared(account: Any) -> bool:
    """True for an Account that Account deletion kept and cleared: inactive,
    with a placeholder email (ADR 0048). Deletion is final, so nobody
    changes one (#344)."""
    return bool(not account.is_active and is_deleted_email(account.email))


def _stored_is_cleared(account: Any) -> bool:
    """``_is_cleared`` for the stored row. The change view asks for the
    change permission on the instance the form has written its data on,
    so a refused save would otherwise show as read-only, without its
    errors."""
    if account.pk is None:
        return False
    stored = type(account)._default_manager.filter(pk=account.pk).first()
    return stored is not None and _is_cleared(stored)


PLACEHOLDER_EMAIL_KEPT_MESSAGE = (
    "This Account has a deleted Account's address, which cannot be changed."
)
PLACEHOLDER_ACTIVE_MESSAGE = (
    "An Account with a deleted Account's address stays inactive."
)


def _placeholder_errors(
    form: forms.ModelForm[Any], cleaned: dict[str, Any], stored_email: str | None
) -> None:
    """Refuse an Account that would look cleared without Account deletion
    clearing it (#344). Only ``delete_account`` writes a placeholder
    address (``is_deleted_email``): an Account that looks cleared cannot be
    changed or deleted, so staff typing one would keep its personal data
    for good. An Account that already has one (an Account reactivated
    before #344) keeps its address and is saved inactive. Runs in the
    form's ``clean()``, on the address normalized as the model normalizes
    it; an address with a field error is not checked again."""

    def refuse(field: str, message: str) -> None:
        form.add_error(field if field in form.fields else None, message)

    email = stored_email or ""
    changed = False
    if "email" in cleaned:
        email = normalize_email_address(cleaned["email"])
        changed = email != stored_email
    if stored_email is not None and is_deleted_email(stored_email):
        if changed:
            refuse("email", PLACEHOLDER_EMAIL_KEPT_MESSAGE)
        if cleaned.get("is_active", form.instance.is_active):
            refuse("is_active", PLACEHOLDER_ACTIVE_MESSAGE)
    elif changed and is_deleted_email(email):
        refuse("email", PLACEHOLDER_EMAIL_MESSAGE)


class AccountCreationForm(UserCreationForm):
    """The Account add form, refusing a placeholder address (#344)."""

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        _placeholder_errors(self, cleaned, None)
        return cleaned


class AccountChangeForm(UserChangeForm):
    """The Account change form, refusing a new grant of staff or superuser
    status to an Account without TOTP (#101, ADR 0056: the admin's login
    takes only codes, so a passkey alone does not count), with fixed text
    that names no Account. A grant the Account already holds is not
    checked again. It also refuses a placeholder address
    (``_placeholder_errors``, #344)."""

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        for name in ("is_staff", "is_superuser"):
            # Before _post_clean, the instance still holds the stored values.
            newly_granted = cleaned.get(name) and not getattr(self.instance, name)
            if newly_granted and not has_totp(self.instance):
                self.add_error(name, GRANT_NEEDS_MFA_MESSAGE)
        _placeholder_errors(self, cleaned, self.instance.email or None)
        return cleaned


@admin.register(User)
class UserAdmin(AccessAuditAdminMixin, BaseUserAdmin[User]):
    form = AccountChangeForm
    add_form = AccountCreationForm
    change_password_form = AdminPasswordChangeForm
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
        """Only a superuser changes an Account that holds power (#101).
        Nobody, superusers included, changes an Account that Account
        deletion cleared (#344, ADR 0048): it stays inactive, with its
        placeholder email and unusable password. Staff who may view
        Accounts still see it, read-only."""
        if obj is not None:
            if _stored_is_cleared(obj):
                return False
            if _holds_power(obj) and not _is_superuser(request):
                return False
        return super().has_change_permission(request, obj)

    def _refuse_change_to_stored(self, request: HttpRequest, pk: Any) -> None:
        """Lock the stored Account row and check the change permission on
        it again, inside the caller's transaction.

        The change page and the set-password page check the Account they
        loaded, which a deletion (or a grant) may have changed since: a
        stale form would write back the old email, ``is_active``, and
        password hash, or insert a hard-deleted Account again.
        ``delete_account`` locks the row first too, so either the deletion
        waits for this change, or this sees it and refuses. It refuses
        nothing else: a change form still saves every field it loaded.
        """
        # FOR NO KEY UPDATE: it waits for delete_account's FOR UPDATE, but
        # not for the FOR KEY SHARE that a new row naming the Account (an
        # Audit Event's actor) takes, so two admin saves cannot deadlock on
        # it.
        locked = (
            self.model._default_manager.select_for_update(no_key=True)
            .filter(pk=pk)
            .first()
        )
        if (
            locked is None
            or _is_cleared(locked)
            or not self.has_change_permission(request, locked)
        ):
            raise PermissionDenied

    def save_model(
        self, request: HttpRequest, obj: User, form: Any, change: bool
    ) -> None:
        """Save a change only when the stored Account may still be changed
        (``_refuse_change_to_stored``). Django's change view runs this in
        its transaction, so the lock lasts until the change commits."""
        if change:
            self._refuse_change_to_stored(request, obj.pk)
        super().save_model(request, obj, form, change)

    # Each Account is deleted through delete_model, in its own transaction
    # (AdminAuditMixin records its admin.delete there), never all of a
    # selection in one: delete_account locks the Account, its Player, and
    # its Organizers until the transaction ends (#317).
    deletes_each_through_delete_model = True

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Nobody deletes a staff or superuser Account here, as the API
        refuses them (#317, ADR 0048), or an Account a deletion already
        cleared. Only a superuser deletes an Account that holds a group or
        a permission (#101)."""
        if obj is not None:
            if obj.is_staff or obj.is_superuser:
                return False
            if _is_cleared(obj):
                return False
            if _holds_power(obj) and not _is_superuser(request):
                return False
        return super().has_delete_permission(request, obj)

    def get_deleted_objects(
        self, objs: Any, request: HttpRequest
    ) -> tuple[list[Any], dict[Any, int], set[str], list[Any]]:
        """What the confirmation pages list: the selected Accounts only.

        A delete here runs ``delete_account``, not Django's cascade, so
        Django's collector would list rows the service keeps and call a
        protected Account undeletable. The service keeps such an Account,
        cleared and deactivated (ADR 0048). An Account the actor may not
        delete (``has_delete_permission``) blocks the whole selection.
        """
        accounts = list(objs)
        refused = any(not self.has_delete_permission(request, a) for a in accounts)
        perms_needed = {str(self.opts.verbose_name)} if refused else set()
        model_count = {self.opts.verbose_name_plural: len(accounts)}
        return [str(a) for a in accounts], model_count, perms_needed, []

    def log_deletions(self, request: HttpRequest, queryset: Any) -> list[LogEntry]:
        """Write no admin log entry: its ``object_repr`` is the Account's
        email (ADR 0009), and ``delete_account`` deletes every entry tied
        to the Account. The ``admin.delete`` and ``account.delete`` Audit
        Events record the deletion."""
        return []

    def delete_model(self, request: HttpRequest, obj: User) -> None:
        """Delete the Account's personal data through ``delete_account``,
        with the staff member as actor, as ``DELETE /api/v1/accounts/me``
        does (#317, ADR 0048).

        The Account row is locked and checked again first, so one made
        staff since the page loaded is refused; ``delete_account`` then
        takes the same lock. An Account deleted meanwhile is the same as
        success, as in the API, but then nothing was deleted here, so the
        ``admin.delete`` event ``AdminAuditMixin`` recorded first, in the
        transaction around this call, is rolled back.
        """
        deleted = False
        with transaction.atomic():
            locked = (
                type(obj)._default_manager.select_for_update().filter(pk=obj.pk).first()
            )
            if locked is not None:
                if not self.has_delete_permission(request, locked):
                    raise PermissionDenied
                with suppress(AccountAlreadyDeleted):
                    delete_account(locked, actor=request.user)
                    deleted = True
        if not deleted and transaction.get_connection().in_atomic_block:
            transaction.set_rollback(True)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        """Delete each selected Account through ``delete_model``, in its own
        transaction.

        ``delete_selected`` already refuses a selection with an Account the
        actor may not delete on its confirmation page; this checks the
        stored rows again before the first delete, and ``delete_model``
        checks each under its lock, so an Account changed in between stops
        the run there with the Accounts before it deleted.
        """
        accounts = list(queryset.order_by("pk"))
        if any(not self.has_delete_permission(request, a) for a in accounts):
            raise PermissionDenied
        for account in accounts:
            self.delete_model(request, account)

    def response_delete(
        self, request: HttpRequest, obj_display: str, obj_id: Any
    ) -> HttpResponse:
        """Django's answer, with the row id in the success message instead
        of the deleted Account's email (ADR 0009)."""
        response: HttpResponse = super().response_delete(request, str(obj_id), obj_id)
        return response

    def user_change_password(
        self, request: HttpRequest, id: str, form_url: str = ""
    ) -> HttpResponse:
        """Django's set-password view, in one transaction, so the new
        password and its Audit Event commit together (#101). A new password
        ends the Account's sessions and their user session rows, or, for
        the staff member's own Account, the other sessions (#100). Only when
        the stored password hash really changed: Django also answers 302
        for conflicting form data, which saves nothing. A POST first locks the
        stored Account and checks it may still be changed (#344)."""
        old_key = request.session.session_key
        # Django's own lookup: a malformed or unknown id is None here, and
        # Django's view then answers 404.
        stored = self.get_object(request, unquote(id))
        before = None if stored is None else stored.password
        with transaction.atomic():
            if request.method == "POST" and stored is not None:
                self._refuse_change_to_stored(request, stored.pk)
            response: HttpResponse = super().user_change_password(request, id, form_url)
            if request.method == "POST" and response.status_code == 302:
                account = self.get_object(request, unquote(id))
                if account is not None and account.password != before:
                    end_sessions_after_password_change(request, account, old_key)
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


class PeriodicTaskForm(BasePeriodicTaskForm):  # type: ignore[misc]
    """django_celery_beat's form, with fixed text for ``regtask``, whose
    declared message repeats the value (issue #162)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["regtask"].error_messages.update(CHOICE_ERRORS)


@admin.register(PeriodicTask)
class PeriodicTaskAdmin(_SuperuserWrites, BasePeriodicTaskAdmin):  # type: ignore[misc]
    """django_celery_beat's admin, for superusers only to write (#101).

    DatabaseScheduler sends whatever this table says, so a task added or
    changed here can send any registered task with chosen arguments, as
    often as every second. Adding, changing, deleting, and every action
    (enable, disable, toggle, and run, which sends tasks at once) are
    superuser-only. Viewing stays with the periodic task permissions."""

    form = PeriodicTaskForm

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

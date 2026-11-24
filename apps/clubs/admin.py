"""Admin for Clubs and Organizers.

Verification is read-only on the change form. It changes only through the
verify and unverify actions, which call ``apps.clubs.services`` and are
shown only to Accounts with ``clubs.verify_organizer``. Both need a typed
reason (``ReasonActionForm``), which goes into the Audit Event.
"""

from collections.abc import Callable, Sequence
from typing import Any

from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected as django_delete_selected
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse

from apps.audit.admin_actions import ReasonActionForm, action_reason
from apps.clubs.models import (
    Club,
    ClubRole,
    Organizer,
    OrganizerMember,
    VerifiedOrganizerDelete,
)
from apps.clubs.services import (
    VERIFY_PERMISSION,
    NotAllowedToVerify,
    unverify_organizer,
    verify_organizer,
)

BLANK_REASON = "Give a reason to verify or unverify organizers."


class OrganizerMemberInline(admin.TabularInline[OrganizerMember, Organizer]):
    model = OrganizerMember
    extra = 0
    autocomplete_fields = ("account",)


class ClubRoleInline(admin.TabularInline[ClubRole, Club]):
    model = ClubRole
    extra = 0
    autocomplete_fields = ("account",)


@admin.register(Club)
class ClubAdmin(admin.ModelAdmin[Club]):
    list_display = ("public_code", "name", "city", "region")
    readonly_fields = ("public_code",)
    search_fields = ("public_code", "name")
    inlines = (ClubRoleInline,)


@admin.register(Organizer)
class OrganizerAdmin(admin.ModelAdmin[Organizer]):
    list_display = ("public_code", "name", "is_verified")
    readonly_fields = ("public_code", "verified_at", "verified_by")
    search_fields = ("public_code", "name")
    inlines = (OrganizerMemberInline,)
    actions = ("verify_selected", "unverify_selected", "delete_selected")
    action_form = ReasonActionForm

    def get_deleted_objects(
        self, objs: Sequence[Organizer] | QuerySet[Organizer], request: HttpRequest
    ) -> tuple[list[Any], dict[str, int], set[str], list[Any]]:
        """Mark every verified Organizer as protected.

        The delete view and ``delete_selected`` then show "Cannot delete"
        and delete, log, and report nothing. Unverify it first: that
        records an Audit Event, which a delete would not.
        """
        to_delete, model_count, perms_needed, protected = super().get_deleted_objects(
            objs, request
        )
        protected = list(protected)
        for organizer in objs:
            if organizer.is_verified:
                # The OPEN Code only: the name may be a person's.
                protected.append(
                    f"Organizer {organizer.public_code} is verified: unverify it first."
                )
        return to_delete, model_count, perms_needed, protected

    def delete_queryset(
        self, request: HttpRequest, queryset: QuerySet[Organizer]
    ) -> None:
        """Delete the selection, or nothing if any of it is now verified.

        ``get_deleted_objects`` checked earlier, but a row can be verified
        between that read and this delete, so lock the rows and check
        again. A refusal raises, and ``delete_selected`` rolls back.
        """
        with transaction.atomic():
            pks = list(queryset.values_list("pk", flat=True))
            locked = Organizer.objects.select_for_update().filter(pk__in=pks)
            verified = [o.public_code for o in locked.order_by("pk") if o.is_verified]
            if verified:
                raise VerifiedOrganizerDelete(verified)
            super().delete_queryset(request, queryset)

    @admin.action(
        permissions=["delete"],
        description=django_delete_selected.short_description,  # type: ignore[attr-defined]
    )
    def delete_selected(
        self, request: HttpRequest, queryset: QuerySet[Organizer]
    ) -> HttpResponse | None:
        """Django's delete_selected, in one transaction.

        A refusal from ``delete_queryset`` then also rolls back the
        deletion log entries, and shows a warning instead of a 500.
        """
        try:
            with transaction.atomic():
                response: HttpResponse | None = django_delete_selected(
                    self, request, queryset
                )
                return response
        except VerifiedOrganizerDelete as refused:
            self._warn_verified(request, refused.public_codes)
            return None

    def delete_view(
        self, request: HttpRequest, object_id: str, extra_context: Any = None
    ) -> HttpResponse:
        """Django's delete view, with a refusal shown as a warning.

        ``Organizer.delete()`` refuses a row that is verified when it is
        locked. Django's view runs in a transaction, so the refusal also
        rolls back its deletion log entry.
        """
        try:
            return super().delete_view(request, object_id, extra_context)
        except VerifiedOrganizerDelete as refused:
            self._warn_verified(request, refused.public_codes)
            return HttpResponseRedirect(
                reverse(
                    "admin:clubs_organizer_change",
                    args=[object_id],
                    current_app=self.admin_site.name,
                )
            )

    def _warn_verified(self, request: HttpRequest, public_codes: list[str]) -> None:
        for code in public_codes:
            # The OPEN Code only: the name may be a person's.
            self.message_user(
                request,
                f"{code} was not deleted: it is verified. Unverify it first.",
                level=messages.WARNING,
            )

    def has_verify_permission(self, request: HttpRequest) -> bool:
        """Django shows an action marked ``permissions=["verify"]`` only
        when this is True.
        """
        return request.user.has_perm(VERIFY_PERMISSION)

    @admin.display(boolean=True, description="Verified")
    def is_verified(self, organizer: Organizer) -> bool:
        return organizer.is_verified

    @admin.action(permissions=["verify"], description="Verify selected organizers")
    def verify_selected(
        self, request: HttpRequest, queryset: QuerySet[Organizer]
    ) -> None:
        self._apply(request, queryset, verify_organizer, "verified")

    @admin.action(
        permissions=["verify"], description="Remove verification from selected"
    )
    def unverify_selected(
        self, request: HttpRequest, queryset: QuerySet[Organizer]
    ) -> None:
        self._apply(request, queryset, unverify_organizer, "unverified")

    def _apply(
        self,
        request: HttpRequest,
        queryset: QuerySet[Organizer],
        change: Callable[..., Organizer],
        done: str,
    ) -> None:
        reason = action_reason(request)
        if not reason:
            self.message_user(request, BLANK_REASON, level=messages.WARNING)
            return
        count = 0
        for organizer in queryset.order_by("pk"):
            try:
                result = change(organizer, actor=request.user, reason=reason)
            except NotAllowedToVerify:
                # The OPEN Code only: the name may be a person's.
                self.message_user(
                    request,
                    f"{organizer.public_code} was not changed: you may not "
                    "verify or unverify it.",
                    level=messages.WARNING,
                )
                continue
            except Organizer.DoesNotExist:
                # Deleted after the action loaded it. The OPEN Code only.
                self.message_user(
                    request,
                    f"{organizer.public_code} was not changed: it no longer exists.",
                    level=messages.WARNING,
                )
                continue
            # Only what this call changed: the row may have been changed by
            # someone else after the changelist loaded it.
            if result.verification_changed:
                count += 1
        if count:
            self.message_user(request, f"{count} organizer(s) {done}.")

"""Admin for Clubs, Organizers, and Club Organizer Grants.

Verification is read-only on the change form. It changes only through the
verify and unverify actions, which call ``apps.clubs.services`` and are
shown only to Accounts with ``clubs.verify_organizer``. Both need a typed
reason (``ReasonActionForm``), which goes into the Audit Event. So does
"End new member holds" (#232), which calls
``apps.clubs.services.end_member_holds``.

Organizer Reports (#271) are read-only here. A moderator closes open
ones with the close action, which needs a typed reason and calls
``apps.clubs.reports.close_report``. The report admin leaves out every
report on an Organizer the viewer is a member of. The reporter is shown
only to Accounts with ``clubs.verify_organizer``, and only as their
Player's OPEN Code. The Organizer change page shows a read-only
"Moderator review" section from ``organizer_review`` to Accounts with
both ``clubs.verify_organizer`` and ``clubs.view_organizerreport`` that
are not members of that Organizer.

Organizer Verification Requests (#229) are read-only here, for Accounts
with ``clubs.view_organizerverificationrequest`` (the Moderator group),
leaving out requests on an Organizer the viewer is a member of. A
moderator verifies through the Organizer's verify action, which closes
the open request, or closes one without verifying with the close action,
which needs a typed reason and calls
``apps.clubs.verification_requests.close_verification_request``.
"""

from collections.abc import Callable, Sequence
from typing import Any

from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected as django_delete_selected
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString

from apps.audit.admin_access import AccessAuditAdminMixin
from apps.audit.admin_actions import (
    ReasonActionForm,
    action_reason,
    reason_problem,
)
from apps.audit.admin_audit import audited_by_service
from apps.clubs.models import (
    REPORT_VIEW_PERMISSION,
    VERIFY_PERMISSION,
    Club,
    ClubOrganizerGrant,
    ClubRole,
    Organizer,
    OrganizerMember,
    OrganizerReport,
    OrganizerVerificationRequest,
    VerifiedOrganizerDelete,
)
from apps.clubs.reports import NotAllowedToClose, close_report, organizer_review
from apps.clubs.services import (
    NotAllowedToEndHolds,
    NotAllowedToVerify,
    end_member_holds,
    unverify_organizer,
    verify_organizer,
)
from apps.clubs.verification_requests import (
    NotAllowedToCloseRequest,
    close_verification_request,
)
from apps.players.models import Player

BLANK_REASON = "Give a reason to verify or unverify organizers."
BLANK_CLOSE_REASON = "Give a reason to close reports."
BLANK_REQUEST_CLOSE_REASON = "Give a reason to close verification requests."
BLANK_HOLD_REASON = "Give a reason to end new member holds."
REVIEW_FIELD = "moderator_review"


class OrganizerMemberInline(admin.TabularInline[OrganizerMember, Organizer]):
    model = OrganizerMember
    extra = 0
    autocomplete_fields = ("account",)
    # The New Member Hold (#232): set only by an accepted invite, ended
    # early only by the "End new member holds" action.
    readonly_fields = ("hold_ends_at",)


class ClubRoleInline(admin.TabularInline[ClubRole, Club]):
    model = ClubRole
    extra = 0
    autocomplete_fields = ("account",)


@admin.register(Club)
class ClubAdmin(AccessAuditAdminMixin, admin.ModelAdmin[Club]):
    list_display = ("public_code", "name", "city", "region")
    readonly_fields = ("public_code",)
    search_fields = ("public_code", "name")
    inlines = (ClubRoleInline,)


@admin.register(ClubOrganizerGrant)
class ClubOrganizerGrantAdmin(
    AccessAuditAdminMixin, admin.ModelAdmin[ClubOrganizerGrant]
):
    """A Club's consent for an Organizer to name it (ADR 0021).

    Staff manage grants here; a Club's admins manage their own through
    Club self-service (``apps.clubs.club_self_service``, #230).
    """

    # apps.audit.access records the Audit Events (#101).
    audited_by_service = True
    list_display = ("club", "organizer", "created_at")
    list_select_related = ("club", "organizer")
    raw_id_fields = ("club", "organizer")
    readonly_fields = ("created_at",)


@admin.register(Organizer)
class OrganizerAdmin(AccessAuditAdminMixin, admin.ModelAdmin[Organizer]):
    list_display = ("public_code", "name", "is_verified")
    readonly_fields = ("public_code", "verified_at", "verified_by")
    search_fields = ("public_code", "name")
    inlines = (OrganizerMemberInline,)
    actions = (
        "verify_selected",
        "unverify_selected",
        "end_member_holds_selected",
        "delete_selected",
    )
    action_form = ReasonActionForm

    def _shows_review(self, request: HttpRequest, obj: Organizer | None) -> bool:
        # Never to a member of the Organizer: its open reports would tell
        # the Organizer it was reported, and when.
        return (
            obj is not None
            and self.has_verify_permission(request)
            # Its links open the report admin, which needs this.
            and request.user.has_perm(REPORT_VIEW_PERMISSION)
            and not _is_member(request, obj.pk)
        )

    def get_readonly_fields(
        self, request: HttpRequest, obj: Organizer | None = None
    ) -> tuple[str, ...]:
        fields = tuple(super().get_readonly_fields(request, obj))
        if self._shows_review(request, obj):
            fields += (REVIEW_FIELD,)
        return fields

    def get_fields(
        self, request: HttpRequest, obj: Organizer | None = None
    ) -> list[str | list[str] | tuple[str, ...]]:
        # The review has a fieldset of its own (get_fieldsets).
        return [f for f in super().get_fields(request, obj) if f != REVIEW_FIELD]

    def get_fieldsets(self, request: HttpRequest, obj: Organizer | None = None) -> Any:
        fieldsets = list(super().get_fieldsets(request, obj))
        if self._shows_review(request, obj):
            fieldsets.append(("Moderator review", {"fields": (REVIEW_FIELD,)}))
        return fieldsets

    @admin.display(description="Review")
    def moderator_review(self, organizer: Organizer) -> SafeString:
        """The open reports and the recent dispute counts (#271).

        Reasons, dates, links, and counts only: never a reporter, a name,
        or an email.
        """
        review = organizer_review(organizer)
        reports = format_html_join(
            "",
            '<li><a href="{}">{}</a>, {}</li>',
            (
                (
                    reverse(
                        "admin:clubs_organizerreport_change",
                        args=[report.pk],
                        current_app=self.admin_site.name,
                    ),
                    report.get_reason_display(),
                    report.created_at.date().isoformat(),
                )
                for report in review.open_reports
            ),
        )
        counts = format_html_join(
            "",
            "<li>{}: {}</li>",
            (
                ("Disputes opened", review.disputes_opened),
                ("Disputes withdrawn", review.disputes_withdrawn),
                ("Disputes resolved as confirmed", review.disputes_confirmed),
                ("Disputes resolved as voided", review.disputes_voided),
                ("Disputes expired", review.disputes_expired),
                ("Signal flags", review.signal_flags),
            ),
        )
        return format_html(
            "<p>Open reports: {}</p><ul>{}</ul>"
            "<p>On this organizer's events' matches, in the last {} days:</p>"
            "<ul>{}</ul>",
            len(review.open_reports),
            reports,
            review.window_days,
            counts,
        )

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

    @audited_by_service
    @admin.action(permissions=["verify"], description="Verify selected organizers")
    def verify_selected(
        self, request: HttpRequest, queryset: QuerySet[Organizer]
    ) -> None:
        self._apply(request, queryset, verify_organizer, "verified")

    @audited_by_service
    @admin.action(
        permissions=["verify"], description="Remove verification from selected"
    )
    def unverify_selected(
        self, request: HttpRequest, queryset: QuerySet[Organizer]
    ) -> None:
        self._apply(request, queryset, unverify_organizer, "unverified")

    @audited_by_service
    @admin.action(permissions=["verify"], description="End new member holds")
    def end_member_holds_selected(
        self, request: HttpRequest, queryset: QuerySet[Organizer]
    ) -> None:
        """End the New Member Holds of the selected Organizers (#232)."""
        reason = action_reason(request)
        problem = reason_problem(reason, BLANK_HOLD_REASON)
        if problem is not None:
            self.message_user(request, problem, level=messages.WARNING)
            return
        count = 0
        for organizer in queryset.order_by("pk"):
            try:
                count += end_member_holds(organizer, actor=request.user, reason=reason)
            except NotAllowedToEndHolds:
                # The OPEN Code only: the name may be a person's.
                self.message_user(
                    request,
                    f"{organizer.public_code} was not changed: you may not end "
                    "its new member holds.",
                    level=messages.WARNING,
                )
            except Organizer.DoesNotExist:
                self.message_user(
                    request,
                    f"{organizer.public_code} was not changed: it no longer exists.",
                    level=messages.WARNING,
                )
        if count:
            self.message_user(request, f"{count} new member hold(s) ended.")

    def _apply(
        self,
        request: HttpRequest,
        queryset: QuerySet[Organizer],
        change: Callable[..., Organizer],
        done: str,
    ) -> None:
        reason = action_reason(request)
        problem = reason_problem(reason, BLANK_REASON)
        if problem is not None:
            self.message_user(request, problem, level=messages.WARNING)
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


def _member_organizer_ids(request: HttpRequest) -> QuerySet[OrganizerMember, Any]:
    """The Organizers the signed-in Account is a member of, as a subquery."""
    return OrganizerMember.objects.filter(account_id=request.user.pk).values(
        "organizer_id"
    )


def _is_member(request: HttpRequest, organizer_id: int) -> bool:
    return OrganizerMember.objects.filter(
        account_id=request.user.pk, organizer_id=organizer_id
    ).exists()


class OpenReportFilter(admin.SimpleListFilter):
    """Open (not closed) or closed reports."""

    title = "open"
    parameter_name = "open"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Open"), ("no", "Closed")]

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[OrganizerReport]
    ) -> QuerySet[OrganizerReport]:
        if self.value() == "yes":
            return queryset.filter(closed_at__isnull=True)
        if self.value() == "no":
            return queryset.filter(closed_at__isnull=False)
        return queryset


@admin.register(OrganizerReport)
class OrganizerReportAdmin(admin.ModelAdmin[OrganizerReport]):
    """Reports of verified Organizers to moderators (#271). Read-only.

    Reports on an Organizer the viewer is a member of are left out of the
    list, the change page, and the close action (``get_queryset``). The
    reporter's Account is never shown: ``User.__str__`` is its email.
    Accounts with ``clubs.verify_organizer`` see the reporter's Player's
    OPEN Code; others see no reporter at all. No lookup on the reporter
    is allowed.
    """

    list_display = ("organizer_code", "reason", "created_at", "is_open")
    list_filter = (OpenReportFilter, "reason")
    list_select_related = ("organizer",)
    search_fields = ("organizer__public_code",)
    ordering = ("-created_at", "-pk")
    actions = ("close_selected",)
    action_form = ReasonActionForm

    def get_queryset(self, request: HttpRequest) -> QuerySet[OrganizerReport]:
        """Every report except those on an Organizer the viewer is a member
        of, so a member never reaches who reported it: not in the list, the
        change page, or the close action.
        """
        return (
            super()
            .get_queryset(request)
            .exclude(organizer_id__in=_member_organizer_ids(request))
        )

    def lookup_allowed(self, lookup: str, value: str, request: HttpRequest) -> bool:
        """Refuse every lookup on the reporter (Django answers 400), so no
        one lists the reports one Account made by its row id.
        """
        if lookup.startswith("reporter"):
            return False
        return super().lookup_allowed(lookup, value, request)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: OrganizerReport | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: OrganizerReport | None = None
    ) -> bool:
        return False

    def has_verify_permission(self, request: HttpRequest) -> bool:
        """Django shows an action marked ``permissions=["verify"]`` only
        when this is True.
        """
        return request.user.has_perm(VERIFY_PERMISSION)

    def get_fields(
        self, request: HttpRequest, obj: OrganizerReport | None = None
    ) -> tuple[str, ...]:
        fields: tuple[str, ...] = (
            "organizer_code",
            "reason",
            "created_at",
            "closed_at",
        )
        if self.has_verify_permission(request) and (
            obj is None or not _is_member(request, obj.organizer_id)
        ):
            fields += ("reporter_player",)
        return fields

    def get_readonly_fields(
        self, request: HttpRequest, obj: OrganizerReport | None = None
    ) -> tuple[str, ...]:
        return self.get_fields(request, obj)

    @admin.display(description="Organizer", ordering="organizer__public_code")
    def organizer_code(self, report: OrganizerReport) -> str:
        return report.organizer.public_code

    @admin.display(boolean=True, description="Open")
    def is_open(self, report: OrganizerReport) -> bool:
        return report.is_open

    @admin.display(description="Reporter's player")
    def reporter_player(self, report: OrganizerReport) -> str:
        """The reporter's Player's OPEN Code, never the Account's email."""
        code = (
            Player.objects.filter(account_id=report.reporter_id)
            .values_list("public_code", flat=True)
            .first()
        )
        return code or "-"

    @audited_by_service
    @admin.action(permissions=["verify"], description="Close selected reports")
    def close_selected(
        self, request: HttpRequest, queryset: QuerySet[OrganizerReport]
    ) -> None:
        reason = action_reason(request)
        problem = reason_problem(reason, BLANK_CLOSE_REASON)
        if problem is not None:
            self.message_user(request, problem, level=messages.WARNING)
            return
        count = 0
        for report in queryset.select_related("organizer").order_by("pk"):
            # Built here, not from str(report), which names no Organizer.
            # The OPEN Code only: the name may be a person's.
            label = f"Report on {report.organizer.public_code}"
            try:
                if close_report(report, actor=request.user, reason=reason):
                    count += 1
            except NotAllowedToClose:
                self.message_user(
                    request,
                    f"{label} was not closed: you may not close it.",
                    level=messages.WARNING,
                )
            except OrganizerReport.DoesNotExist:
                # Removed with its Account after the action loaded it.
                self.message_user(
                    request,
                    f"{label} was not closed: it no longer exists.",
                    level=messages.WARNING,
                )
        if count:
            self.message_user(request, f"{count} report(s) closed.")


class OpenRequestFilter(admin.SimpleListFilter):
    """Open (not closed) or closed verification requests."""

    title = "open"
    parameter_name = "open"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Open"), ("no", "Closed")]

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[OrganizerVerificationRequest]
    ) -> QuerySet[OrganizerVerificationRequest]:
        if self.value() == "yes":
            return queryset.filter(closed_at__isnull=True)
        if self.value() == "no":
            return queryset.filter(closed_at__isnull=False)
        return queryset


@admin.register(OrganizerVerificationRequest)
class OrganizerVerificationRequestAdmin(admin.ModelAdmin[OrganizerVerificationRequest]):
    """Requests that moderators verify an Organizer (#229). Read-only.

    Requests on an Organizer the viewer is a member of are left out of
    the list, the change page, and the close action (``get_queryset``),
    so no member reviews their own request. The requesting Account is
    not shown; it is only the create event's actor.
    """

    list_display = ("organizer_code", "created_at", "is_open")
    list_filter = (OpenRequestFilter,)
    list_select_related = ("organizer",)
    search_fields = ("organizer__public_code",)
    ordering = ("-created_at", "-pk")
    actions = ("close_selected",)
    action_form = ReasonActionForm
    fields = ("organizer_code", "statement", "created_at", "closed_at")
    readonly_fields = fields

    def get_queryset(
        self, request: HttpRequest
    ) -> QuerySet[OrganizerVerificationRequest]:
        return (
            super()
            .get_queryset(request)
            .exclude(organizer_id__in=_member_organizer_ids(request))
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: OrganizerVerificationRequest | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: OrganizerVerificationRequest | None = None
    ) -> bool:
        return False

    def has_verify_permission(self, request: HttpRequest) -> bool:
        """Django shows an action marked ``permissions=["verify"]`` only
        when this is True.
        """
        return request.user.has_perm(VERIFY_PERMISSION)

    @admin.display(description="Organizer", ordering="organizer__public_code")
    def organizer_code(self, request: OrganizerVerificationRequest) -> str:
        return request.organizer.public_code

    @admin.display(boolean=True, description="Open")
    def is_open(self, request: OrganizerVerificationRequest) -> bool:
        return request.is_open

    @audited_by_service
    @admin.action(permissions=["verify"], description="Close selected requests")
    def close_selected(
        self, request: HttpRequest, queryset: QuerySet[OrganizerVerificationRequest]
    ) -> None:
        reason = action_reason(request)
        problem = reason_problem(reason, BLANK_REQUEST_CLOSE_REASON)
        if problem is not None:
            self.message_user(request, problem, level=messages.WARNING)
            return
        count = 0
        for verification in queryset.select_related("organizer").order_by("pk"):
            # The OPEN Code only: the name may be a person's.
            label = f"Request on {verification.organizer.public_code}"
            try:
                if close_verification_request(
                    verification, actor=request.user, reason=reason
                ):
                    count += 1
            except NotAllowedToCloseRequest:
                self.message_user(
                    request,
                    f"{label} was not closed: you may not close it.",
                    level=messages.WARNING,
                )
        if count:
            self.message_user(request, f"{count} request(s) closed.")

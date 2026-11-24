"""Admin for Partner Applications and Organizer Application Grants.

django-oauth-toolkit registers its own admin for the swapped Application
model. This module runs after it (``apps.partners`` follows
``oauth2_provider`` in ``INSTALLED_APPS``) and replaces it.

The client secret is shown only on the add form, as the generated value
to copy before saving; it is stored hashed and never shown again.
Approval is read-only: #29's service changes it.
"""

from django.contrib import admin
from django.http import HttpRequest
from oauth2_provider.admin import ApplicationAdmin

from apps.partners.models import OrganizerApplicationGrant, PartnerApplication

# Every secret is hashed (a check constraint), so the choice is never shown.
_ALWAYS_EXCLUDED: tuple[str, ...] = ("hash_client_secret",)

admin.site.unregister(PartnerApplication)


@admin.register(PartnerApplication)
class PartnerApplicationAdmin(ApplicationAdmin):
    list_display = (
        "pk",
        "client_id",
        "name",
        "organizer",
        "client_type",
        "authorization_grant_type",
        "is_sandbox",
        "approved_at",
    )
    list_filter = (
        "is_sandbox",
        "client_type",
        "authorization_grant_type",
        "registration_source",
    )
    list_select_related = ("organizer",)
    raw_id_fields = ("user", "organizer")
    # No public page (PartnerApplication.get_absolute_url).
    view_on_site = False
    readonly_fields = (
        "registration_source",
        "cimd_expires_at",
        "approved_at",
        "approved_by",
    )

    def get_exclude(
        self, request: HttpRequest, obj: PartnerApplication | None = None
    ) -> tuple[str, ...]:
        if obj is None:
            return _ALWAYS_EXCLUDED
        # A saved secret is a hash: never shown, and not replaced here.
        return (*_ALWAYS_EXCLUDED, "client_secret")


@admin.register(OrganizerApplicationGrant)
class OrganizerApplicationGrantAdmin(admin.ModelAdmin[OrganizerApplicationGrant]):
    list_display = ("organizer", "application", "created_at")
    list_select_related = ("organizer", "application")
    raw_id_fields = ("organizer", "application")
    readonly_fields = ("created_at",)

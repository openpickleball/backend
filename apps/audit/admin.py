from django.contrib import admin
from django.http import HttpRequest

from apps.audit.models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin[AuditEvent]):
    """Read-only: audit events are append-only, superusers included.

    Reading needs the ``audit.view_auditevent`` permission (or superuser).
    Adding, changing, and deleting are refused for every request.
    """

    list_display = (
        "created_at",
        "action",
        "actor_user",
        "target_content_type",
        "target_object_id",
    )
    list_filter = ("action", "target_content_type")
    search_fields = ("target_object_id",)
    list_select_related = ("actor_user", "target_content_type")
    date_hierarchy = "created_at"
    # No actions at all, so the bulk "delete selected" action is gone.
    actions = None
    fields = (
        "created_at",
        "action",
        "actor_user",
        "target_content_type",
        "target_object_id",
        "before",
        "after",
        "reason",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: AuditEvent | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: AuditEvent | None = None
    ) -> bool:
        return False

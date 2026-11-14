"""Audit events: an append-only record of sensitive changes.

Write an AuditEvent only through ``apps.audit.services.record_event``.
"""

from typing import ClassVar

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone


class AuditAction(models.TextChoices):
    """Every action an AuditEvent can record.

    A later issue that needs a new action adds a value here, then generates
    the migration for the changed choices with makemigrations.
    """

    ADMIN_ADD = "admin.add", "Admin: add"
    ADMIN_CHANGE = "admin.change", "Admin: change"
    ADMIN_DELETE = "admin.delete", "Admin: delete"
    RATING_EDIT = "rating.edit", "Manual rating edit"
    MATCH_EDIT = "match.edit", "Manual match edit"
    DISPUTE_OUTCOME = "dispute.outcome", "Dispute outcome"
    MFA_CHANGE = "mfa.change", "MFA change"
    PASSWORD_CHANGE = "password.change", "Password change"
    SESSION_END = "session.end", "Session ended by the player"
    OAUTH_APP_REQUEST = "oauth_app.request", "OAuth application request"
    OAUTH_APP_APPROVE = "oauth_app.approve", "OAuth application approval"
    OAUTH_APP_REJECT = "oauth_app.reject", "OAuth application rejection"
    OAUTH_APP_REVOKE = "oauth_app.revoke", "OAuth application revocation"


class AuditEvent(models.Model):
    """One recorded action: who did it, to what, and the before and after."""

    created_at = models.DateTimeField(
        default=timezone.now, db_index=True, editable=False
    )
    # PROTECT, not SET_NULL: SET_NULL would issue an UPDATE, which the
    # append-only trigger refuses. Deactivate an Account that has acted.
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    action = models.CharField(max_length=64, choices=AuditAction.choices, db_index=True)
    target_content_type = models.ForeignKey(
        ContentType, on_delete=models.PROTECT, related_name="+"
    )
    # A string, so integer and UUID primary keys both fit.
    target_object_id = models.CharField(max_length=255)
    # For display only.
    target = GenericForeignKey("target_content_type", "target_object_id")
    before = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    after = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    reason = models.TextField(blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]
        # Only a view permission exists: nobody may add, change, or delete.
        default_permissions = ("view",)
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["target_content_type", "target_object_id"]),
        ]
        verbose_name = "audit event"

    def __str__(self) -> str:
        return (
            f"{self.get_action_display()}: {self.target_content_type.model} "
            f"{self.target_object_id} at {self.created_at.isoformat()}"
        )

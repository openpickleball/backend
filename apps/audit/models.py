"""Audit events: an append-only record of sensitive changes.

Write an AuditEvent only through ``apps.audit.services.record_event``.
"""

from collections.abc import Collection, Iterable
from typing import Any, ClassVar, NoReturn

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models.signals import pre_save
from django.dispatch import receiver
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


class AuditEventImmutable(RuntimeError):
    """Raised when code tries to change or delete an AuditEvent."""


_IMMUTABLE_MESSAGE = "Audit events are append-only: they cannot be changed or deleted."


class AuditEventQuerySet(models.QuerySet["AuditEvent"]):
    """Refuses bulk changes and deletes before any SQL runs."""

    def update(self, **kwargs: Any) -> NoReturn:
        raise AuditEventImmutable(_IMMUTABLE_MESSAGE)

    def delete(self) -> NoReturn:
        raise AuditEventImmutable(_IMMUTABLE_MESSAGE)

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AuditEventImmutable(_IMMUTABLE_MESSAGE)

    def bulk_create(
        self,
        objs: Iterable["AuditEvent"],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list["AuditEvent"]:
        # An upsert would update an existing row.
        if update_conflicts:
            raise AuditEventImmutable(_IMMUTABLE_MESSAGE)
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class AuditEvent(models.Model):
    """One recorded action: who did it, to what, and the before and after.

    Rows are append-only. The ORM refuses changes here, and a database
    trigger (migration 0002) refuses UPDATE, DELETE, and TRUNCATE of a
    non-empty table.
    """

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

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]
        # Only a view permission exists: nobody may add, change, or delete.
        default_permissions = ("view",)
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["target_content_type", "target_object_id"]),
        ]
        verbose_name = "audit event"
        # Django's internal queries (related lookups, deletion collection)
        # use the base manager, so it must be the guarded one too.
        base_manager_name = "objects"

    def __str__(self) -> str:
        return (
            f"{self.get_action_display()}: {self.target_content_type.model} "
            f"{self.target_object_id} at {self.created_at.isoformat()}"
        )

    def save_base(
        self,
        raw: bool = False,
        force_insert: bool | tuple[models.base.ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        # save() and create() go through here. The pre_save receiver below
        # applies the same rule to callers that skip this override.
        if force_update:
            raise AuditEventImmutable(_IMMUTABLE_MESSAGE)
        _refuse_unless_new(self, update_fields)
        super().save_base(
            raw=raw,
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AuditEventImmutable(_IMMUTABLE_MESSAGE)


def _refuse_unless_new(
    instance: AuditEvent, update_fields: Iterable[str] | None
) -> None:
    """Refuse any save that could update an existing row.

    Only a brand new instance with no primary key may be saved, and then
    only as a plain insert.
    """
    if (
        not instance._state.adding
        or instance.pk is not None
        or update_fields is not None
    ):
        raise AuditEventImmutable(_IMMUTABLE_MESSAGE)


@receiver(pre_save, sender=AuditEvent)
def _refuse_audit_event_changes(
    sender: type[AuditEvent],
    instance: AuditEvent,
    update_fields: Iterable[str] | None = None,
    **kwargs: Any,
) -> None:
    """Apply the rule to every save, raw ones included.

    Deserialization (loaddata) calls ``models.Model.save_base`` directly,
    which skips the override on AuditEvent but still sends pre_save before
    any SQL runs.
    """
    _refuse_unless_new(instance, update_fields)

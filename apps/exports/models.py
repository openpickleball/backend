"""The player data export (issue #39, ADR 0051).

An Account has at most one ``DataExport``. A request stores a pending
row; the build task fills in the JSON (``content``, encrypted with the
field encryption keys, ADR 0046), the SHA-256 of the emailed download
key (``key_hash``), and the build and expiry times. The first successful
download deletes the row, and so do a new request, the sweep once the
row expires, and Account deletion. The key itself is never stored.

No admin is registered, and ``__str__`` never shows the Account.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.encryption.fields import EncryptedTextField


class ExportStatus(models.TextChoices):
    """Whether the export has been built."""

    PENDING = "pending", "Pending"
    READY = "ready", "Ready"


class DataExport(models.Model):
    """One Account's data export, waiting to be built or downloaded."""

    account = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="data_export",
    )
    status = models.CharField(
        max_length=16, choices=ExportStatus.choices, default=ExportStatus.PENDING
    )
    # The export's JSON, a Fernet token in the column. Empty until built.
    content = EncryptedTextField(null=True, blank=True)
    # The SHA-256 hex digest of the download key. Empty until built.
    key_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    requested_at = models.DateTimeField(default=timezone.now)
    # When the build task was last queued: the sweep's lease.
    queued_at = models.DateTimeField(default=timezone.now, db_index=True)
    built_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = "data export"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(status__in=ExportStatus.values),
                name="exports_dataexport_status_valid",
            ),
            models.CheckConstraint(
                condition=~Q(status=ExportStatus.READY)
                | Q(
                    content__isnull=False,
                    key_hash__isnull=False,
                    built_at__isnull=False,
                    expires_at__isnull=False,
                ),
                name="exports_dataexport_ready_is_complete",
            ),
        ]

    def __str__(self) -> str:
        return f"DataExport {self.pk}"

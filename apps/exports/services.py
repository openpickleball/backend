"""Request, build, download, and sweep player data exports (issue #39,
ADR 0051).

- ``request_export(account)`` locks the Account row (``FOR NO KEY
  UPDATE``, so two requests run in turn and Account deletion's ``FOR
  UPDATE`` waits or is waited on), deletes any earlier export of the
  Account, stores a pending one, records one ``data_export.request``
  Audit Event, and queues ``build_data_export`` with the row id after
  commit.
- ``build_data_export(export_id)`` holds the Account (``FOR KEY SHARE``,
  checking it is active), locks the export row ``FOR UPDATE``, and
  builds only a pending row: the JSON (encrypted by its field), the
  download key's SHA-256, the build and expiry times, and one email with
  the link, in one transaction. Run twice, it builds and mails once.
- ``download_export(account, key)`` finds a ready, unexpired export of
  that Account by the key's hash under ``FOR UPDATE``, records one
  ``data_export.download`` Audit Event, deletes the row, and returns the
  JSON. Every refusal is ``ExportNotFound``, so the API answers one 404.
- ``sweep_data_exports()`` deletes expired exports and pending ones older
  than the link lifetime, and queues again pending ones whose task was
  lost (queued more than ``REQUEUE_AFTER`` ago).

Lock order: the Account row, then the export row, as Account deletion
takes them. The key exists only in the email (the outbox row, deleted
once sent) and the player's mailbox; it is never logged, stored, or put
in a task argument or Audit Event.
"""

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from kombu.exceptions import OperationalError

from apps.accounts.locks import hold_active_account
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.emails.rendering import render_email
from apps.emails.services import queue_email
from apps.exports.builder import build_export
from apps.exports.models import DataExport, ExportStatus

if TYPE_CHECKING:
    from django.contrib.auth.models import _User
    from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)

# How long a download link works after the export is built.
LINK_LIFETIME: Final = timedelta(hours=24)
# How long a pending export waits before the sweep queues its build again.
REQUEUE_AFTER: Final = timedelta(hours=1)
# The longest key the download accepts; the build makes 43 characters.
KEY_MAX_LENGTH: Final = 128
DATA_EXPORT_READY: Final = "exports/email/data_export_ready"

_BROKER_ERRORS = (OperationalError, OSError)


class ExportNotFound(LookupError):
    """No export for this request: the API answers one fixed 404."""

    def __init__(self) -> None:
        super().__init__("No data export was found.")


class DataExportFailed(RuntimeError):
    """A build failed in a worker. Fixed text: the row id and error class."""

    def __init__(self, export_id: int, error_class: str) -> None:
        super().__init__(f"Data export {export_id} failed to build ({error_class}).")


def hash_key(key: str) -> str:
    """The stored form of a download key: its SHA-256 hex digest."""
    return hashlib.sha256(key.encode()).hexdigest()


def export_url(key: str) -> str:
    """The web app page that downloads the export with ``key``."""
    return f"{settings.FRONTEND_URL}/account/export/{key}"


def request_export(account: "_User") -> DataExport:
    """Replace ``account``'s export with a new pending one and queue its build.

    Raises ``ExportNotFound`` for an inactive or missing Account, and
    writes nothing then. Call it outside any other transaction.
    """
    with transaction.atomic():
        locked = (
            get_user_model()
            .objects.select_for_update(no_key=True)
            .filter(pk=account.pk, is_active=True)
            .first()
        )
        if locked is None:
            raise ExportNotFound
        DataExport.objects.filter(account_id=locked.pk).delete()
        export = DataExport.objects.create(account=locked)
        record_event(
            action=AuditAction.DATA_EXPORT_REQUEST, target=locked, actor=locked
        )
        transaction.on_commit(partial(_enqueue, export.pk))
    return export


def build_data_export(export_id: int, *, now: datetime | None = None) -> bool:
    """Build the pending export with this row id and mail its link.

    Returns True when it built, False when there was nothing to do: the
    row is gone, already built, or its Account is inactive.
    """
    account_id = (
        DataExport.objects.filter(pk=export_id).values_list("account_id", flat=True)
    ).first()
    if account_id is None:
        return False
    with transaction.atomic():
        if not hold_active_account(account_id):
            return False
        export = (
            DataExport.objects.select_for_update()
            .filter(pk=export_id, account_id=account_id)
            .first()
        )
        if export is None or export.status != ExportStatus.PENDING:
            return False
        account = get_user_model().objects.get(pk=account_id)
        built_at = now or timezone.now()
        key = secrets.token_urlsafe(32)
        export.content = build_export(account, now=built_at).model_dump_json()
        export.key_hash = hash_key(key)
        export.status = ExportStatus.READY
        export.built_at = built_at
        export.expires_at = built_at + LINK_LIFETIME
        export.save(
            update_fields=["content", "key_hash", "status", "built_at", "expires_at"]
        )
        queue_email(
            render_export_ready(account, key=key, expires_at=export.expires_at),
            template=DATA_EXPORT_READY,
        )
    return True


def render_export_ready(
    account: "_User", *, key: str, expires_at: datetime
) -> "EmailMultiAlternatives":
    """The email with the download link, to the Account's login address."""
    return render_email(
        DATA_EXPORT_READY,
        str(account.email),
        {
            "export_url": export_url(key),
            "expires_at": expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        },
    )


def download_export(account: "_User", key: str) -> str:
    """Return the export's JSON once, deleting it; ``ExportNotFound`` otherwise.

    The export must be ready, unexpired, and ``account``'s. A key that is
    empty or longer than ``KEY_MAX_LENGTH`` is refused before any lookup.
    """
    if not key or len(key) > KEY_MAX_LENGTH:
        raise ExportNotFound
    with transaction.atomic():
        export = (
            DataExport.objects.select_for_update()
            .filter(
                key_hash=hash_key(key),
                account_id=account.pk,
                status=ExportStatus.READY,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if export is None or export.content is None:
            raise ExportNotFound
        content = str(export.content)
        record_event(
            action=AuditAction.DATA_EXPORT_DOWNLOAD, target=account, actor=account
        )
        export.delete()
    return content


def delete_exports_for(account_id: int) -> None:
    """Delete the Account's export (Account deletion, under its lock)."""
    DataExport.objects.filter(account_id=account_id).delete()


def sweep_data_exports(*, now: datetime | None = None) -> int:
    """Delete expired and stale exports and queue lost builds again.

    Returns how many builds it queued.
    """
    now = now or timezone.now()
    DataExport.objects.filter(status=ExportStatus.READY, expires_at__lte=now).delete()
    DataExport.objects.filter(
        status=ExportStatus.PENDING, requested_at__lt=now - LINK_LIFETIME
    ).delete()
    stale = DataExport.objects.filter(
        status=ExportStatus.PENDING, queued_at__lt=now - REQUEUE_AFTER
    )
    queued = 0
    for export_id in stale.values_list("pk", flat=True):
        with transaction.atomic():
            if stale.filter(pk=export_id).update(queued_at=now):
                transaction.on_commit(partial(_enqueue, export_id))
                queued += 1
    return queued


def _enqueue(export_id: int) -> None:
    """Queue the build. A broker error is logged by id and class only; the
    sweep queues the build again."""
    from apps.exports.tasks import build_data_export as build_task

    try:
        build_task.delay(export_id)
    except _BROKER_ERRORS as exc:
        logger.error(
            "Could not queue data export %s (%s).", export_id, type(exc).__name__
        )

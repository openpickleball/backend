"""The stored data export (issue #39, ADR 0051). Made-up data only."""

import json
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.encryption.keys import decrypt_text, is_token
from apps.exports.models import DataExport, ExportStatus

pytestmark = pytest.mark.django_db

CONTENT = json.dumps({"made_up": "Vexmoor export content"})


def _raw_content(export: DataExport) -> str | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT content FROM exports_dataexport WHERE id = %s", [export.pk]
        )
        value: str | None = cursor.fetchone()[0]
    return value


def test_a_new_export_is_pending_and_empty() -> None:
    export = DataExport.objects.create(account=UserFactory.create())

    export.refresh_from_db()
    assert export.status == ExportStatus.PENDING
    assert export.content is None
    assert export.key_hash is None
    assert export.built_at is None
    assert export.expires_at is None
    assert export.requested_at is not None
    assert export.queued_at is not None


def test_the_content_is_stored_as_a_fernet_token() -> None:
    export = DataExport.objects.create(account=UserFactory.create())
    export.content = CONTENT
    export.save(update_fields=["content"])

    raw = _raw_content(export)
    assert raw is not None
    assert is_token(raw)
    assert "Vexmoor" not in raw
    assert decrypt_text(raw) == CONTENT
    export.refresh_from_db()
    assert export.content == CONTENT


def test_one_export_per_account() -> None:
    account = UserFactory.create()
    DataExport.objects.create(account=account)

    with pytest.raises(IntegrityError), transaction.atomic():
        DataExport.objects.create(account=account)


def test_the_key_hash_is_unique() -> None:
    first = DataExport.objects.create(account=UserFactory.create(), key_hash="a" * 64)

    with pytest.raises(IntegrityError), transaction.atomic():
        DataExport.objects.create(account=UserFactory.create(), key_hash=first.key_hash)


def test_the_database_refuses_an_unknown_status() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        DataExport.objects.create(account=UserFactory.create(), status="lost")


def test_a_ready_export_needs_its_content_key_and_times() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        DataExport.objects.create(
            account=UserFactory.create(), status=ExportStatus.READY
        )


def test_a_ready_export_with_everything_is_stored() -> None:
    now = timezone.now()
    export = DataExport.objects.create(
        account=UserFactory.create(),
        status=ExportStatus.READY,
        content=CONTENT,
        key_hash="b" * 64,
        built_at=now,
        expires_at=now + timedelta(hours=24),
    )

    assert DataExport.objects.filter(pk=export.pk, status=ExportStatus.READY).exists()


def test_str_never_names_the_account() -> None:
    account = UserFactory.create()
    export = DataExport.objects.create(account=account)

    assert account.email not in str(export)
    assert str(export) == f"DataExport {export.pk}"

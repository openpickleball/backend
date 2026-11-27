"""Requesting, building, downloading, and sweeping data exports (issue #39,
ADR 0051). Made-up data only.
"""

import json
import logging
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from celery.schedules import crontab
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.utils import timezone
from kombu.serialization import dumps

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.emails.models import QueuedEmail
from apps.encryption.keys import is_token
from apps.exports import services, tasks
from apps.exports.models import DataExport, ExportStatus
from apps.exports.services import (
    LINK_LIFETIME,
    REQUEUE_AFTER,
    ExportNotFound,
    build_data_export,
    download_export,
    hash_key,
    request_export,
    sweep_data_exports,
)
from apps.players.tests.factories import PlayerFactory
from config.celery import app as celery_app

pytestmark = pytest.mark.django_db

LEGAL_NAME = "Thessaly Made-up Exporter"
SECRET = "made-up error text Vexmoor"
_LINK = re.compile(r"/account/export/([A-Za-z0-9_-]+)")


class MadeUpError(RuntimeError):
    pass


@pytest.fixture
def no_queue() -> Iterator[mock.MagicMock]:
    """Keep the build task from running when on_commit callbacks run."""
    with mock.patch.object(tasks.build_data_export, "delay") as delay:
        yield delay


def _key_from_email() -> str:
    (message,) = mail.outbox
    found = _LINK.search(str(message.body))
    assert found is not None
    return found.group(1)


def _built(account: Any, capture: Any) -> str:
    """Request and build an export for ``account``; return the emailed key."""
    mail.outbox.clear()
    with capture(execute=True):
        request_export(account)
    return _key_from_email()


def _events(action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action))


# Request


def test_a_request_stores_a_pending_export_and_one_event(
    no_queue: mock.MagicMock, django_capture_on_commit_callbacks: Any
) -> None:
    account = UserFactory.create()

    with django_capture_on_commit_callbacks(execute=True):
        export = request_export(account)

    export.refresh_from_db()
    assert export.status == ExportStatus.PENDING
    assert export.content is None
    no_queue.assert_called_once_with(export.pk)
    (event,) = _events(AuditAction.DATA_EXPORT_REQUEST)
    assert event.actor_user_id == account.pk
    assert event.target_object_id == str(account.pk)
    assert event.before is None
    assert event.after is None
    assert event.reason == ""


def test_a_new_request_replaces_the_earlier_export(
    no_queue: mock.MagicMock, django_capture_on_commit_callbacks: Any
) -> None:
    account = UserFactory.create()
    first = request_export(account)

    second = request_export(account)

    assert not DataExport.objects.filter(pk=first.pk).exists()
    assert list(DataExport.objects.filter(account=account)) == [second]


def test_an_inactive_account_cannot_request(no_queue: mock.MagicMock) -> None:
    account = UserFactory.create(is_active=False)

    with pytest.raises(ExportNotFound):
        request_export(account)

    assert not DataExport.objects.exists()
    assert _events(AuditAction.DATA_EXPORT_REQUEST) == []


def test_the_task_gets_only_the_row_id(
    no_queue: mock.MagicMock, django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        export = request_export(UserFactory.create())

    assert no_queue.call_args.args == (export.pk,)
    assert no_queue.call_args.kwargs == {}
    dumps(no_queue.call_args.args, serializer="json")


# Build


def test_the_build_stores_encrypted_json_and_mails_the_link(
    django_capture_on_commit_callbacks: Any,
) -> None:
    player = PlayerFactory.create(legal_name=LEGAL_NAME)
    account = player.account

    key = _built(account, django_capture_on_commit_callbacks)

    export = DataExport.objects.get(account=account)
    assert export.status == ExportStatus.READY
    assert export.key_hash == hash_key(key)
    assert export.built_at is not None
    assert export.expires_at == export.built_at + LINK_LIFETIME
    assert json.loads(str(export.content))["profile"]["legal_name"] == LEGAL_NAME
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT content, key_hash FROM exports_dataexport WHERE id = %s",
            [export.pk],
        )
        raw, stored_hash = cursor.fetchone()
    assert is_token(raw)
    assert LEGAL_NAME not in raw
    assert key not in stored_hash
    (message,) = mail.outbox
    assert message.to == [account.email]
    assert f"{settings.FRONTEND_URL}/account/export/{key}" in str(message.body)
    assert not message.attachments
    assert not QueuedEmail.objects.exists()


def test_the_build_runs_once_when_run_twice(
    django_capture_on_commit_callbacks: Any,
) -> None:
    account = UserFactory.create()
    _built(account, django_capture_on_commit_callbacks)
    export = DataExport.objects.get(account=account)
    built_at, key_hash = export.built_at, export.key_hash

    with django_capture_on_commit_callbacks(execute=True):
        assert build_data_export(export.pk) is False

    export.refresh_from_db()
    assert (export.built_at, export.key_hash) == (built_at, key_hash)
    assert len(mail.outbox) == 1


def test_a_missing_row_writes_nothing() -> None:
    assert build_data_export(987654) is False
    assert not QueuedEmail.objects.exists()


def test_an_inactive_account_gets_no_build(no_queue: mock.MagicMock) -> None:
    account = UserFactory.create()
    export = request_export(account)
    get_user_model().objects.filter(pk=account.pk).update(is_active=False)

    assert build_data_export(export.pk) is False

    export.refresh_from_db()
    assert export.status == ExportStatus.PENDING
    assert not QueuedEmail.objects.exists()


def test_the_build_task_routes_to_default_under_the_visibility_timeout() -> None:
    task = tasks.build_data_export
    assert task.name == "apps.exports.tasks.build_data_export"
    assert celery_app.amqp.router.route({}, task.name)["queue"].name == "default"
    assert task.soft_time_limit == 240
    assert task.time_limit == 300
    assert timedelta(seconds=task.time_limit) < REQUEUE_AFTER


def _run_build(export_id: int, *, is_eager: bool) -> Any:
    task = tasks.build_data_export
    task.push_request(is_eager=is_eager)
    try:
        return task.run(export_id)
    finally:
        task.pop_request()


def test_a_worker_failure_logs_and_raises_fixed_text_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    no_queue: mock.MagicMock,
) -> None:
    account = UserFactory.create()
    export = request_export(account)

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise MadeUpError(SECRET)

    monkeypatch.setattr(services, "build_export", _fail)

    with (
        caplog.at_level(logging.INFO, logger="apps.exports"),
        pytest.raises(services.DataExportFailed) as caught,
    ):
        _run_build(export.pk, is_eager=False)

    assert SECRET not in str(caught.value)
    assert "MadeUpError" in str(caught.value)
    assert caught.value.__suppress_context__ is True
    (line,) = [r for r in caplog.records if r.name == "apps.exports.tasks"]
    assert str(export.pk) in line.getMessage()
    assert "MadeUpError" in line.getMessage()
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert account.email not in record.getMessage()
        assert record.exc_info is None
    export.refresh_from_db()
    assert export.status == ExportStatus.PENDING


def test_an_eager_failure_raises_the_original_error(
    monkeypatch: pytest.MonkeyPatch, no_queue: mock.MagicMock
) -> None:
    export = request_export(UserFactory.create())

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise MadeUpError(SECRET)

    monkeypatch.setattr(services, "build_export", _fail)

    with pytest.raises(MadeUpError):
        _run_build(export.pk, is_eager=True)


def test_the_task_returns_nothing(no_queue: mock.MagicMock) -> None:
    export = request_export(UserFactory.create())

    assert _run_build(export.pk, is_eager=False) is None


# Download


def test_the_download_returns_the_json_once_and_deletes_the_row(
    django_capture_on_commit_callbacks: Any,
) -> None:
    account = UserFactory.create()
    key = _built(account, django_capture_on_commit_callbacks)

    content = download_export(account, key)

    assert json.loads(content)["account"]["email"] == account.email
    assert not DataExport.objects.filter(account=account).exists()
    (event,) = _events(AuditAction.DATA_EXPORT_DOWNLOAD)
    assert event.actor_user_id == account.pk
    assert event.target_object_id == str(account.pk)
    assert (event.before, event.after, event.reason) == (None, None, "")
    with pytest.raises(ExportNotFound):
        download_export(account, key)


def test_another_accounts_key_is_not_found(
    django_capture_on_commit_callbacks: Any,
) -> None:
    owner, other = UserFactory.create(), UserFactory.create()
    key = _built(owner, django_capture_on_commit_callbacks)

    with pytest.raises(ExportNotFound):
        download_export(other, key)

    assert DataExport.objects.filter(account=owner).exists()
    assert _events(AuditAction.DATA_EXPORT_DOWNLOAD) == []


def test_an_expired_key_is_not_found(
    django_capture_on_commit_callbacks: Any,
) -> None:
    account = UserFactory.create()
    key = _built(account, django_capture_on_commit_callbacks)
    DataExport.objects.filter(account=account).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    with pytest.raises(ExportNotFound):
        download_export(account, key)


@pytest.mark.parametrize("key", ["", "x", "a" * 129, "not a key at all"])
def test_an_unknown_or_malformed_key_is_not_found(key: str) -> None:
    with pytest.raises(ExportNotFound):
        download_export(UserFactory.create(), key)


def test_a_pending_export_is_not_found(no_queue: mock.MagicMock) -> None:
    account = UserFactory.create()
    request_export(account)

    with pytest.raises(ExportNotFound):
        download_export(account, "made-up-key-for-a-pending-export")


# Sweep


def _age(export: DataExport, **fields: Any) -> None:
    DataExport.objects.filter(pk=export.pk).update(**fields)


def test_the_sweep_deletes_expired_and_stale_pending_exports(
    no_queue: mock.MagicMock, django_capture_on_commit_callbacks: Any
) -> None:
    now = timezone.now()
    expired = DataExport.objects.create(
        account=UserFactory.create(),
        status=ExportStatus.READY,
        content="{}",
        key_hash="c" * 64,
        built_at=now - LINK_LIFETIME - timedelta(minutes=1),
        expires_at=now - timedelta(minutes=1),
    )
    fresh = DataExport.objects.create(
        account=UserFactory.create(),
        status=ExportStatus.READY,
        content="{}",
        key_hash="d" * 64,
        built_at=now,
        expires_at=now + LINK_LIFETIME,
    )
    stale = DataExport.objects.create(account=UserFactory.create())
    _age(stale, requested_at=now - LINK_LIFETIME - timedelta(minutes=1))

    with django_capture_on_commit_callbacks(execute=True):
        sweep_data_exports()

    assert list(DataExport.objects.all()) == [fresh]
    no_queue.assert_not_called()
    assert expired.pk


def test_the_sweep_queues_a_lost_pending_export_again_once(
    no_queue: mock.MagicMock, django_capture_on_commit_callbacks: Any
) -> None:
    now = timezone.now()
    lost = DataExport.objects.create(account=UserFactory.create())
    _age(lost, queued_at=now - REQUEUE_AFTER - timedelta(minutes=1))
    DataExport.objects.create(account=UserFactory.create())

    with django_capture_on_commit_callbacks(execute=True):
        assert sweep_data_exports() == 1
    with django_capture_on_commit_callbacks(execute=True):
        assert sweep_data_exports() == 0

    no_queue.assert_called_once_with(lost.pk)


def test_the_sweep_task_and_its_beat_entry() -> None:
    task = tasks.sweep_data_exports
    assert task.name == "apps.exports.tasks.sweep_data_exports"
    assert celery_app.amqp.router.route({}, task.name)["queue"].name == "default"
    entry = settings.CELERY_BEAT_SCHEDULE["sweep-data-exports"]
    assert entry["task"] == task.name
    assert entry["options"] == {"expire_seconds": 900}
    assert entry["schedule"] == crontab(minute="*/15")

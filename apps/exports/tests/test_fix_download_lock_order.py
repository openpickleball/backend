"""The download locks the Account row before the export row (#39).

``download_export`` locked the export row ``FOR UPDATE`` and then
recorded its Audit Event, whose deferred actor key takes the Account
``FOR KEY SHARE`` at commit, while Account deletion holds the Account
``FOR UPDATE`` and then deletes the export row: a concurrent download
and deletion of one Account deadlocked. The download now holds the
Account first (``hold_active_account``), as the build does, and an
inactive Account gets ``ExportNotFound``. Made-up data only.
"""

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.tests.factories import UserFactory
from apps.exports.models import DataExport
from apps.exports.services import ExportNotFound, download_export, request_export
from apps.exports.tests.test_services import _key_from_email

pytestmark = pytest.mark.django_db

ACCOUNT_TABLE = f'"{get_user_model()._meta.db_table}"'
EXPORT_TABLE = '"exports_dataexport"'


def _key(django_capture_on_commit_callbacks: Any, account: Any) -> str:
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        request_export(account)
    return _key_from_email()


def test_the_download_holds_the_account_before_the_export_row(
    django_capture_on_commit_callbacks: Any,
) -> None:
    account = UserFactory.create()
    key = _key(django_capture_on_commit_callbacks, account)

    with CaptureQueriesContext(connection) as queries:
        download_export(account, key)

    sql = [query["sql"] for query in queries.captured_queries]
    account_locks = [
        i
        for i, text in enumerate(sql)
        if ACCOUNT_TABLE in text and "FOR KEY SHARE" in text
    ]
    export_locks = [
        i for i, text in enumerate(sql) if EXPORT_TABLE in text and "FOR UPDATE" in text
    ]
    assert account_locks, sql
    assert export_locks, sql
    assert account_locks[0] < export_locks[0], sql


def test_an_inactive_account_cannot_download(
    django_capture_on_commit_callbacks: Any,
) -> None:
    account = UserFactory.create()
    key = _key(django_capture_on_commit_callbacks, account)
    get_user_model().objects.filter(pk=account.pk).update(is_active=False)

    with pytest.raises(ExportNotFound):
        download_export(account, key)

    assert DataExport.objects.filter(account=account).exists()

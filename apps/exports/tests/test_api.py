"""``POST /api/v1/me/export`` and ``POST /api/v1/me/export/download``
(issue #39, ADR 0051).

The request needs a recent reauthentication and has its own day
throttle; the build runs after commit and mails a single-use key; the
download needs the signed-in Account and that key, and answers every
refusal with one 404. Made-up data only.
"""

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.core.cache import cache
from django.test import Client, override_settings

from apps.accounts.tests.factories import DEFAULT_PASSWORD, UserFactory
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession, csrf_header
from apps.audit.models import AuditAction, AuditEvent
from apps.exports.api import DataExportThrottle
from apps.exports.models import DataExport
from apps.exports.schemas import EXPORT_FILENAME
from apps.players.tests.factories import PlayerFactory
from config.throttling import DAY, FixedWindowThrottle

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

REQUEST = "/api/v1/me/export"
DOWNLOAD = "/api/v1/me/export/download"
CLIENTS = [APP, BROWSER]
NOW = 1_800_000_000.0
_LINK = re.compile(r"/account/export/([A-Za-z0-9_-]+)")
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cache.clear()
    monkeypatch.setattr(FixedWindowThrottle, "timer", staticmethod(lambda: NOW))
    yield
    cache.clear()


def _signed_in(base: str, account: Any = None) -> tuple[HeadlessSession, Any]:
    account = account or UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    session = HeadlessSession(base)
    assert session.login(account.email, DEFAULT_PASSWORD).status_code == 200
    return session, account


def _headers(session: HeadlessSession) -> dict[str, str]:
    if session.base == BROWSER:
        return csrf_header(session.client)
    return {"X-Session-Token": session.token or ""}


def _post(session: HeadlessSession, path: str, data: Any = None) -> Any:
    return session.client.post(
        path,
        json.dumps(data if data is not None else {}),
        content_type="application/json",
        headers=_headers(session),
    )


def _reauthenticated(base: str, account: Any = None) -> tuple[HeadlessSession, Any]:
    session, account = _signed_in(base, account)
    assert session.reauthenticate(DEFAULT_PASSWORD).status_code == 200
    return session, account


def _emailed_key() -> str:
    found = _LINK.search(str(mail.outbox[-1].body))
    assert found is not None
    return found.group(1)


def test_signed_out_is_a_401(client: Client) -> None:
    assert client.post(REQUEST).status_code == 401
    response = client.post(DOWNLOAD, {"key": "x"}, content_type="application/json")
    assert response.status_code == 401


@pytest.mark.parametrize("base", CLIENTS)
def test_without_a_reauthentication_the_request_is_a_403(base: str) -> None:
    session, _ = _signed_in(base)

    response = _post(session, REQUEST)

    assert response.status_code == 403
    assert response.json()["code"] == "reauthentication_required"
    assert not DataExport.objects.exists()
    assert not AuditEvent.objects.filter(action=AuditAction.DATA_EXPORT_REQUEST)


@pytest.mark.parametrize("base", CLIENTS)
def test_request_build_and_download_once(base: str) -> None:
    player = PlayerFactory.create()
    session, account = _reauthenticated(base, player.account)

    requested = _post(session, REQUEST)

    assert requested.status_code == 202
    assert set(requested.json()) == {"requested_at"}
    key = _emailed_key()
    downloaded = _post(session, DOWNLOAD, {"key": key})
    assert downloaded.status_code == 200
    assert downloaded["Content-Type"] == "application/json"
    assert (
        downloaded["Content-Disposition"] == f'attachment; filename="{EXPORT_FILENAME}"'
    )
    assert "no-store" in downloaded["Cache-Control"]
    data = json.loads(downloaded.content)
    assert data["account"]["email"] == account.email
    assert data["profile"]["code"] == player.public_code
    assert not DataExport.objects.exists()
    again = _post(session, DOWNLOAD, {"key": key})
    assert again.status_code == 404
    assert again.json() == NOT_FOUND
    assert (
        AuditEvent.objects.filter(action=AuditAction.DATA_EXPORT_REQUEST).count() == 1
    )
    assert (
        AuditEvent.objects.filter(action=AuditAction.DATA_EXPORT_DOWNLOAD).count() == 1
    )


def test_the_download_needs_no_reauthentication() -> None:
    session, _ = _reauthenticated(APP)
    assert _post(session, REQUEST).status_code == 202
    key = _emailed_key()
    later = HeadlessSession(APP)
    account = DataExport.objects.get().account
    assert later.login(account.email, DEFAULT_PASSWORD).status_code == 200

    assert _post(later, DOWNLOAD, {"key": key}).status_code == 200


@pytest.mark.parametrize(
    "key", ["", "made-up-unknown-key", "a" * 129, "x" * 5000, "not a key"]
)
def test_an_unknown_or_malformed_key_is_the_same_404(key: str) -> None:
    session, _ = _signed_in(APP)

    response = _post(session, DOWNLOAD, {"key": key})

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_another_accounts_key_is_the_same_404() -> None:
    owner, _ = _reauthenticated(APP)
    assert _post(owner, REQUEST).status_code == 202
    key = _emailed_key()
    other, _ = _signed_in(APP)

    response = _post(other, DOWNLOAD, {"key": key})

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert DataExport.objects.exists()


def test_a_missing_key_is_a_422() -> None:
    session, _ = _signed_in(APP)

    assert _post(session, DOWNLOAD, {}).status_code == 422


def test_the_fourth_request_in_a_day_is_429() -> None:
    session, _ = _reauthenticated(APP)
    for _ in range(3):
        assert _post(session, REQUEST).status_code == 202

    refused = _post(session, REQUEST)

    assert refused.status_code == 429
    assert refused.json()["code"] == "throttled"
    assert 1 <= int(refused["Retry-After"]) <= DAY
    assert (
        AuditEvent.objects.filter(action=AuditAction.DATA_EXPORT_REQUEST).count() == 3
    )


@override_settings(API_DATA_EXPORT_THROTTLE_PER_DAY=1)
def test_the_day_limit_comes_from_settings() -> None:
    session, _ = _reauthenticated(APP)

    assert _post(session, REQUEST).status_code == 202
    assert _post(session, REQUEST).status_code == 429


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1, API_ACCOUNT_THROTTLE_PER_DAY=1)
def test_requests_do_not_use_the_account_budget() -> None:
    session, _ = _reauthenticated(APP)

    assert _post(session, REQUEST).status_code == 202
    assert _post(session, REQUEST).status_code == 202


def test_the_throttle_counts_by_account_over_a_day() -> None:
    (window,) = DataExportThrottle("data_export").limits()

    assert window == ("day", 3, DAY)

"""The signed-in Account's data export, at /api/v1/me/export (issue #39,
ADR 0051).

``POST /me/export`` (``request_data_export``) needs a recent
reauthentication (403 ``reauthentication_required`` otherwise, nothing
written), replaces any earlier export, and answers 202; the build runs on
the default queue and emails a single-use link to
``{FRONTEND_URL}/account/export/{key}``. That page calls ``POST
/me/export/download`` (``download_data_export``) with the key, which
returns the JSON once as a file and deletes it. A key that is unknown,
malformed, too long, expired, used, not yet built, or another Account's
gets one fixed 404.

The request has its own throttle, ``DataExportThrottle`` (a day window
from ``API_DATA_EXPORT_THROTTLE_PER_DAY``), which replaces the router's
Account throttle (ADR 0030). Only a request with a recent
reauthentication counts against it; one without counts against the
Account throttle's windows. The download uses the Account throttle.
"""

import threading

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from ninja import Router, Status

from apps.exports.schemas import (
    EXPORT_FILENAME,
    DataExportDownloadIn,
    DataExportOut,
    DataExportRequestOut,
)
from apps.exports.services import ExportNotFound, download_export, request_export
from apps.matches.api import request_account
from config.api_errors import ErrorResponse
from config.auth import (
    account_auth,
    recently_reauthenticated,
    require_recent_reauthentication,
)
from config.throttling import (
    DAY,
    FixedWindowThrottle,
    Windows,
    account_bucket,
    account_throttle,
)

me_router = Router(tags=["data export"], auth=account_auth, throttle=account_throttle)


class DataExportThrottle(FixedWindowThrottle):
    """Per-Account export requests per day, from
    ``API_DATA_EXPORT_THROTTLE_PER_DAY``.

    One day window, counted by the Account's primary key, never its
    email. The rate is read on each request. Only a request with a recent
    reauthentication counts here: one without (which the view refuses
    with 403 ``reauthentication_required``) is counted against the
    Account throttle's windows instead, so a refusal never spends an
    export request and refusals are still limited.
    """

    def __init__(self, scope: str) -> None:
        super().__init__(scope)
        self._delegated = threading.local()

    def bucket(self, request: HttpRequest) -> str | None:
        return account_bucket(request)

    def limits(self) -> Windows:
        return (("day", settings.API_DATA_EXPORT_THROTTLE_PER_DAY, DAY),)

    def allow_request(self, request: HttpRequest) -> bool:
        delegated = not recently_reauthenticated(request)
        self._delegated.value = delegated
        if delegated:
            return account_throttle.allow_request(request)
        return super().allow_request(request)

    def wait(self) -> float | None:
        if getattr(self._delegated, "value", False):
            return account_throttle.wait()
        return super().wait()


@me_router.post(
    "/export",
    response={202: DataExportRequestOut, 403: ErrorResponse},
    operation_id="request_data_export",
    throttle=DataExportThrottle("data_export"),
)
def request_data_export(request: HttpRequest) -> Status[DataExportRequestOut]:
    """Ask for a copy of your data.

    Reauthenticate first, through allauth's `auth/reauthenticate` (your
    password) or `auth/2fa/reauthenticate` (a two-factor code); without a
    recent one you get 403 with code `reauthentication_required`. A new
    request replaces any earlier export. Once the export is built, an
    email to your login address links to the web app's download page,
    which calls `download_data_export` with the key. The link works once
    and lasts 24 hours. Limited per Account to a few requests a day; past
    that the answer is 429 with a Retry-After header.
    """
    account = request_account(request)
    require_recent_reauthentication(request)
    try:
        export = request_export(account)
    except ExportNotFound:
        raise Http404 from None
    return Status(202, DataExportRequestOut(requested_at=export.requested_at))


@me_router.post(
    "/export/download",
    response={200: DataExportOut, 404: ErrorResponse},
    operation_id="download_data_export",
)
def download_data_export(
    request: HttpRequest, payload: DataExportDownloadIn
) -> HttpResponse:
    """Download your data export once, with the key from the email.

    Returns the export as a JSON file (`open-data-export.json`) and
    deletes it, so the key works once. A key that is unknown, malformed,
    longer than 128 characters, expired, already used, not built yet, or
    another Account's gets the same 404. No reauthentication is needed.
    """
    account = request_account(request)
    try:
        content = download_export(account, payload.key)
    except ExportNotFound:
        raise Http404 from None
    response = HttpResponse(content, content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="{EXPORT_FILENAME}"'
    response["Cache-Control"] = "no-store"
    return response

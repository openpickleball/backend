"""Reporting an Organizer to OPEN Moderators, at /api/v1/organizers/ (#271).

A signed-in Account with a Player reports a verified Organizer with one
of the fixed reasons, through ``apps.clubs.reports.report_organizer``.
Every refusal (an unknown or malformed code, an unverified Organizer, an
Account with no Player) gets the same 404 as an unknown code (ADR 0008,
ADR 0015), so the answer never says whether an Organizer exists or is
verified. A second open report by the same Account gets 409.

The operation has its own throttle, ``OrganizerReportThrottle``, which
replaces the router's Account throttle (ADR 0030): a report never counts
against the Account's budget for other operations.
"""

from typing import Annotated

from django.conf import settings
from django.http import Http404, HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.clubs.models import Organizer, OrganizerReportReason
from apps.clubs.reports import ReportExists, ReportRefused, report_organizer
from apps.clubs.schemas import OrganizerReportIn, OrganizerReportOut
from apps.matches.api import request_account
from apps.public_ids.api import PublicCodePath, get_by_public_code
from apps.public_ids.codes import CodeType
from config.api_errors import ErrorResponse
from config.auth import account_auth
from config.throttling import (
    DAY,
    FixedWindowThrottle,
    Windows,
    account_bucket,
    account_throttle,
)

router = Router(tags=["organizers"], auth=account_auth, throttle=account_throttle)

OrganizerCode = Annotated[str, PublicCodePath(CodeType.ORGANIZER)]

# Fixed text, never a submitted value.
EXISTS_MESSAGE = "You already have an open report on this organizer."


class OrganizerReportThrottle(FixedWindowThrottle):
    """Per-Account reports per day, from ``API_ORGANIZER_REPORT_THROTTLE_PER_DAY``.

    One day window, counted by the Account's primary key, never its
    email. The rate is read on each request.
    """

    def bucket(self, request: HttpRequest) -> str | None:
        return account_bucket(request)

    def limits(self) -> Windows:
        return (("day", settings.API_ORGANIZER_REPORT_THROTTLE_PER_DAY, DAY),)


@router.post(
    "/{code}/reports",
    response={201: OrganizerReportOut, 404: ErrorResponse, 409: ErrorResponse},
    operation_id="report_organizer",
    throttle=OrganizerReportThrottle("organizer_report"),
)
def report_organizer_operation(
    request: HttpRequest, code: OrganizerCode, payload: OrganizerReportIn
) -> Status[OrganizerReportOut]:
    """Report a verified Organizer to OPEN Moderators.

    For a signed-in Account with a Player. 404 for an Organizer that is
    unknown or not verified, and for an Account with no Player; 409 while
    the Account's earlier report on it is open. Limited per Account to a
    few reports a day; past that the answer is 429 with a Retry-After
    header. The Organizer never learns who reported it.
    """
    account = request_account(request)
    organizer = get_by_public_code(Organizer.objects.only("pk"), code)
    try:
        report = report_organizer(
            organizer.pk, actor=account, reason=OrganizerReportReason(payload.reason)
        )
    except ReportRefused:
        raise Http404 from None
    except ReportExists:
        raise HttpError(409, EXISTS_MESSAGE) from None
    return Status(
        201,
        OrganizerReportOut(
            organizer=organizer.public_code,
            reason=OrganizerReportReason(report.reason),
            created_at=report.created_at,
        ),
    )

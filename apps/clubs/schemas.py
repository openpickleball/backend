"""The API's request and response schemas for Organizer Reports (#271).

No schema here carries a row id or names the reporter: the Organizer is
named by its OPEN Code. Unknown keys are refused, so a report carries no
free text.
"""

from datetime import datetime

from ninja import Schema
from pydantic import ConfigDict

from apps.clubs.models import OrganizerReportReason


class OrganizerReportIn(Schema):
    """A report of a verified Organizer to OPEN Moderators.

    ``reason`` is one of the listed reasons. There is no free text.
    """

    model_config = ConfigDict(extra="forbid")

    reason: OrganizerReportReason


class OrganizerReportOut(Schema):
    """The report just made: the Organizer's OPEN Code, the reason, and
    when it was made. It never names the reporter."""

    organizer: str
    reason: OrganizerReportReason
    created_at: datetime

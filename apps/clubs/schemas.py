"""The API's request and response schemas for Organizer Reports (#271)
and the signed-in Account's own Organizer memberships (#301).

No schema here carries a row id or names the reporter: the Organizer is
named by its OPEN Code. Unknown keys are refused, so a report carries no
free text.
"""

from datetime import datetime

from ninja import Schema
from pydantic import ConfigDict

from apps.clubs.models import MemberRole, OrganizerReportReason


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


class OrganizerMembershipOut(Schema):
    """One of the signed-in Account's own Organizer memberships.

    ``organizer`` is the Organizer's OPEN Code, the value
    ``EventCreateIn.organizer`` takes. ``name`` is the Organizer's name,
    shown only to its own members. ``verified`` says whether the Organizer
    is verified; it does not say whether the Account may act for it now
    (Organizer authority also needs MFA, ADR 0043). ``role`` is the
    Account's own role there. It names no other member.
    """

    organizer: str
    name: str
    verified: bool
    role: MemberRole

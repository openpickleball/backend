"""The API's request and response schemas for Organizer Reports (#271),
the signed-in Account's own Organizer memberships (#301), Organizer
self-service (#229), and Club self-service (#230).

No schema here carries a row id, an email, or names the reporter: an
Organizer and a Club are named by their OPEN Codes and a member or Club
admin by its Player's OPEN Code. A Club grant list names the Organizer
by its OPEN Code only, never its name. Unknown keys are refused, so a report carries no free text.
Validation messages are fixed text and never repeat the submitted value.
"""

import unicodedata
from collections.abc import Callable
from datetime import datetime
from typing import Final

from ninja import Field, Schema
from pydantic import ConfigDict, field_validator
from pydantic_core import PydanticCustomError

from apps.accounts.validators import EMAIL_MAX_LENGTH
from apps.clubs.invites import (
    InvalidInviteEmail,
    normalized_invite_email,
)
from apps.clubs.models import MemberRole, OrganizerReportReason

NAME_MAX_LENGTH: Final = 200
PLACE_MAX_LENGTH: Final = 100
STATEMENT_MAX_LENGTH: Final = 1000
_NAME_REQUIRED = "Enter a name."
_NAME_TOO_LONG = f"Keep the name to {NAME_MAX_LENGTH} characters or fewer."
_STATEMENT_REQUIRED = "Describe what the organizer runs."
_STATEMENT_TOO_LONG = (
    f"Keep the statement to {STATEMENT_MAX_LENGTH} characters or fewer."
)
_CONTROL_CHARACTER = "Remove control or invalid characters."
# Control characters and lone surrogates, as the models refuse them.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})
# A statement may have line breaks and tabs.
_STATEMENT_ALLOWED = frozenset({"\n", "\r", "\t"})


def _has_refused(value: str, allowed: frozenset[str] = frozenset()) -> bool:
    return any(
        unicodedata.category(char) in _REFUSED_CATEGORIES and char not in allowed
        for char in value
    )


def _place(label: str) -> Callable[[object], str]:
    """A check for a Club's city or region: text of at most
    ``PLACE_MAX_LENGTH`` characters after trimming, empty allowed, with no
    control characters. ``label`` names the field in the fixed text."""
    too_long = f"Keep the {label} to {PLACE_MAX_LENGTH} characters or fewer."

    def check(value: object) -> str:
        if not isinstance(value, str):
            raise PydanticCustomError("string_type", "Enter text.")
        value = value.strip()
        if len(value) > PLACE_MAX_LENGTH:
            raise PydanticCustomError("too_long", too_long)
        if _has_refused(value):
            raise PydanticCustomError("invalid_characters", _CONTROL_CHARACTER)
        return value

    return check


_city_text = _place("city")
_region_text = _place("region")


def _invite_email(value: object) -> str:
    if not isinstance(value, str):
        raise PydanticCustomError("string_type", "Enter text.")
    try:
        return normalized_invite_email(value)
    except InvalidInviteEmail:
        raise PydanticCustomError("invalid_email", str(InvalidInviteEmail())) from None


def _organizer_name(value: object) -> str:
    if not isinstance(value, str):
        raise PydanticCustomError("string_type", "Enter text.")
    value = value.strip()
    if not value:
        raise PydanticCustomError("required", _NAME_REQUIRED)
    if len(value) > NAME_MAX_LENGTH:
        raise PydanticCustomError("too_long", _NAME_TOO_LONG)
    if _has_refused(value):
        raise PydanticCustomError("invalid_characters", _CONTROL_CHARACTER)
    return value


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
    (Organizer authority also needs MFA, ADR 0043).
    ``verification_requested`` says whether a request that moderators
    verify it is open (#229). ``role`` is the Account's own role there.
    It names no other member.
    """

    organizer: str
    name: str
    verified: bool
    verification_requested: bool
    role: MemberRole


class OrganizerCreateIn(Schema):
    """A new Organizer. ``name`` is 1 to 200 characters after trimming,
    with no control characters. It may be a person's name: it is shown
    only to the Organizer's members and staff."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=NAME_MAX_LENGTH)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> str:
        return _organizer_name(value)


class OrganizerUpdateIn(Schema):
    """A new name for an Organizer, with the create's rules."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=NAME_MAX_LENGTH)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> str:
        return _organizer_name(value)


class OrganizerInviteIn(Schema):
    """An invite of an email address to act for the Organizer as ``role``."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=EMAIL_MAX_LENGTH)
    role: MemberRole

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, value: object) -> str:
        return _invite_email(value)


class OrganizerInviteSentOut(Schema):
    """The same answer for every accepted invite request: it never says
    whether the address has an Account, is a member, or was invited."""

    message: str


class OrganizerInviteAcceptIn(Schema):
    """The key from the invite email's link."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        description=(
            "The key from the link in the invite email. A key longer than "
            "128 characters gets the same 404 as an unknown one."
        ),
    )


class OrganizerMemberOut(Schema):
    """One member of an Organizer, as its admins see it.

    ``player`` is the member's Player's OPEN Code (the value the member
    operations take), or null when the Account has no Player.
    ``display_name`` is that Player's display name, or null when there is
    none or the Account is inactive. ``you`` marks the signed-in Account.
    Never an email.
    """

    player: str | None
    display_name: str | None
    role: MemberRole
    you: bool


class OrganizerMemberRoleIn(Schema):
    """A member's new role."""

    model_config = ConfigDict(extra="forbid")

    role: MemberRole


class OrganizerVerificationRequestIn(Schema):
    """A request that moderators verify the Organizer.

    ``statement`` says what the Organizer runs, for moderators only: 1 to
    1000 characters after trimming, with no control characters except
    line breaks and tabs.
    """

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(max_length=STATEMENT_MAX_LENGTH)

    @field_validator("statement", mode="before")
    @classmethod
    def _statement(cls, value: object) -> str:
        if not isinstance(value, str):
            raise PydanticCustomError("string_type", "Enter text.")
        value = value.strip()
        if not value:
            raise PydanticCustomError("required", _STATEMENT_REQUIRED)
        if len(value) > STATEMENT_MAX_LENGTH:
            raise PydanticCustomError("too_long", _STATEMENT_TOO_LONG)
        if _has_refused(value, _STATEMENT_ALLOWED):
            raise PydanticCustomError("invalid_characters", _CONTROL_CHARACTER)
        return value


class OrganizerVerificationRequestOut(Schema):
    """The request just made: the Organizer's OPEN Code and when. The
    statement is not repeated."""

    organizer: str
    created_at: datetime


class ClubOut(Schema):
    """A Club's public profile: its OPEN Code (``club``), name, city, and
    region. Any signed-in Account may read it. Never a row id or an
    admin."""

    club: str
    name: str
    city: str
    region: str


class ClubCreateIn(Schema):
    """A new Club. ``name`` is 1 to 200 characters after trimming;
    ``city`` and ``region`` are optional, at most 100 characters each.
    None has control characters. A Club is a place or group, not a
    person: its profile is shown to any signed-in Account."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=NAME_MAX_LENGTH)
    city: str = Field("", max_length=PLACE_MAX_LENGTH)
    region: str = Field("", max_length=PLACE_MAX_LENGTH)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> str:
        return _organizer_name(value)

    @field_validator("city", mode="before")
    @classmethod
    def _city(cls, value: object) -> str:
        return _city_text(value)

    @field_validator("region", mode="before")
    @classmethod
    def _region(cls, value: object) -> str:
        return _region_text(value)


class ClubUpdateIn(Schema):
    """The profile fields to change, with the create's rules. A field left
    out or null is not changed; an empty body changes nothing. ``city``
    and ``region`` may be set to an empty string; ``name`` may not."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, max_length=NAME_MAX_LENGTH)
    city: str | None = Field(None, max_length=PLACE_MAX_LENGTH)
    region: str | None = Field(None, max_length=PLACE_MAX_LENGTH)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> str | None:
        return None if value is None else _organizer_name(value)

    @field_validator("city", mode="before")
    @classmethod
    def _city(cls, value: object) -> str | None:
        return None if value is None else _city_text(value)

    @field_validator("region", mode="before")
    @classmethod
    def _region(cls, value: object) -> str | None:
        return None if value is None else _region_text(value)


class ClubInviteIn(Schema):
    """An invite of an email address to be an admin of the Club."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=EMAIL_MAX_LENGTH)

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, value: object) -> str:
        return _invite_email(value)


class ClubInviteSentOut(Schema):
    """The same answer for every accepted Club invite request: it never
    says whether the address has an Account, is an admin, or was
    invited."""

    message: str


class ClubInviteAcceptIn(OrganizerInviteAcceptIn):
    """The key from the Club invite email's link."""


class ClubAdminOut(Schema):
    """One admin of a Club, as its admins see it.

    ``player`` is the admin's Player's OPEN Code (the value the remove
    operation takes), or null when the Account has no Player.
    ``display_name`` is that Player's display name, or null when there is
    none or the Account is inactive. ``you`` marks the signed-in Account.
    Never an email.
    """

    player: str | None
    display_name: str | None
    you: bool


class ClubOrganizerGrantIn(Schema):
    """The Organizer to allow, by its OPEN Code, in any case, with or
    without hyphens. An unknown or malformed code gets the same 404 as an
    unknown Club."""

    model_config = ConfigDict(extra="forbid")

    organizer: str


class ClubOrganizerGrantOut(Schema):
    """An Organizer the Club allows to name it: the Organizer's OPEN Code
    and when the Club allowed it. Never the Organizer's name, which may be
    a person's."""

    organizer: str
    created_at: datetime


class OrganizerClubGrantOut(Schema):
    """A Club that allows the Organizer to name it, as the Organizer's
    admin members see it: the Club's OPEN Code, name, city, and region,
    and when it allowed it."""

    club: str
    name: str
    city: str
    region: str
    created_at: datetime

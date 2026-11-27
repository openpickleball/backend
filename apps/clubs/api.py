"""Reporting an Organizer to OPEN Moderators, at /api/v1/organizers/ (#271),
and the signed-in Account's own Organizer memberships, at
/api/v1/organizer/organizers (#301, ADR 0050).

A signed-in Account with a Player reports a verified Organizer with one
of the fixed reasons, through ``apps.clubs.reports.report_organizer``.
Every refusal (an unknown or malformed code, an unverified Organizer, an
Account with no Player) gets the same 404 as an unknown code (ADR 0008,
ADR 0015), so the answer never says whether an Organizer exists or is
verified. A second open report by the same Account gets 409.

The operation has its own throttle, ``OrganizerReportThrottle``, which
replaces the router's Account throttle (ADR 0030): a report never counts
against the Account's budget for other operations.

``organizer_router`` (mounted at /api/v1/organizer/, beside the Event and
results file operations) lists the Organizers the signed-in Account is a
member of, verified or not, each with its OPEN Code, name, verified
state, whether a verification request is open, and the Account's own
role. It reads only the Account's own membership rows, so it never names
another member.

Organizer self-service (#229, ADR 0054), also on ``organizer_router``,
through ``apps.clubs.self_service``:

- ``POST /organizer/organizers`` (``create_organizer``): an Account whose
  login address is verified (403 otherwise) creates an unverified
  Organizer and is its only admin; 409 past the per-Account limit.
- The operations with an Organizer code in the path act for an admin
  member: rename, invites, the member list, a member's role and removal,
  and the verification request. An Account without MFA gets 403
  ``mfa_required`` before any database lookup, the same for every
  well-formed code (ADR 0043); a malformed code is already a 404 when
  ``PublicCodePath`` reads the path. A non-member, a match manager, and
  an unknown or malformed code (or member Player code) all get the same
  404.
- ``POST /organizer/invites/accept`` (``accept_organizer_invite``): the
  invited Account joins; every refusal is one 404.

The invite has its own throttle, ``OrganizerInviteThrottle``, which
replaces the router's Account throttle, since each invite sends an email.

``GET /organizer/organizers/{code}/club-grants``
(``list_organizer_club_grants``, #230) lists the Clubs that allow the
Organizer to name them, for an admin member with MFA, by the same rules.
Club self-service itself is in ``apps.clubs.club_api``.
"""

from typing import TYPE_CHECKING, Annotated

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Exists, OuterRef
from django.http import Http404, HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.accounts.mfa import has_mfa
from apps.clubs import self_service
from apps.clubs.models import (
    MemberRole,
    Organizer,
    OrganizerMember,
    OrganizerReportReason,
    OrganizerVerificationRequest,
)
from apps.clubs.reports import ReportExists, ReportRefused, report_organizer
from apps.clubs.schemas import (
    OrganizerClubGrantOut,
    OrganizerCreateIn,
    OrganizerInviteAcceptIn,
    OrganizerInviteIn,
    OrganizerInviteSentOut,
    OrganizerMemberOut,
    OrganizerMemberRoleIn,
    OrganizerMembershipOut,
    OrganizerReportIn,
    OrganizerReportOut,
    OrganizerUpdateIn,
    OrganizerVerificationRequestIn,
    OrganizerVerificationRequestOut,
)
from apps.matches.api import request_account
from apps.public_ids.api import PublicCodePath, get_by_public_code
from apps.public_ids.codes import CodeType
from config.api_errors import ErrorResponse, model_validation_error
from config.auth import MFARequired, account_auth, require_step_up
from config.throttling import (
    DAY,
    FixedWindowThrottle,
    Windows,
    account_bucket,
    account_throttle,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

router = Router(tags=["organizers"], auth=account_auth, throttle=account_throttle)
organizer_router = Router(
    tags=["organizers"], auth=account_auth, throttle=account_throttle
)

OrganizerCode = Annotated[str, PublicCodePath(CodeType.ORGANIZER)]
PlayerCode = Annotated[str, PublicCodePath(CodeType.PLAYER)]

# Fixed text, never a submitted value.
EXISTS_MESSAGE = "You already have an open report on this organizer."
INVITE_SENT_MESSAGE = "If the address can be invited, an invite email is on its way."


class OrganizerInviteThrottle(FixedWindowThrottle):
    """Per-Account invites per day, from ``API_ORGANIZER_INVITE_THROTTLE_PER_DAY``.

    One day window, counted by the Account's primary key, never its
    email. The rate is read on each request.
    """

    def bucket(self, request: HttpRequest) -> str | None:
        return account_bucket(request)

    def limits(self) -> Windows:
        return (("day", settings.API_ORGANIZER_INVITE_THROTTLE_PER_DAY, DAY),)


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


@organizer_router.get(
    "/organizers",
    response={200: list[OrganizerMembershipOut]},
    operation_id="list_my_organizers",
)
def list_my_organizers(request: HttpRequest) -> list[OrganizerMembershipOut]:
    """The Organizers the signed-in Account is a member of.

    Verified or not, each with its OPEN Code (the value an Event create
    takes as ``organizer``), its name, whether it is verified, and the
    Account's own role there. Ordered by OPEN Code. Empty for an Account
    with no memberships. No other member is listed. ``verified`` is the
    Organizer's state: acting for a verified Organizer also needs MFA on
    the Account (ADR 0043).
    """
    account = request_account(request)
    return _memberships(account)


def _memberships(
    account: "_User", organizer_id: int | None = None
) -> list[OrganizerMembershipOut]:
    """The Account's own memberships, ordered by OPEN Code, or only the one
    at ``organizer_id``."""
    memberships = OrganizerMember.objects.filter(account=account)
    if organizer_id is not None:
        memberships = memberships.filter(organizer_id=organizer_id)
    rows = (
        memberships.select_related("organizer")
        .only(
            "role",
            "organizer__public_code",
            "organizer__name",
            "organizer__verified_at",
        )
        .annotate(
            verification_requested=Exists(
                OrganizerVerificationRequest.objects.filter(
                    organizer_id=OuterRef("organizer_id"), closed_at__isnull=True
                )
            )
        )
        .order_by("organizer__public_code")
    )
    return [
        OrganizerMembershipOut(
            organizer=membership.organizer.public_code,
            name=membership.organizer.name,
            verified=membership.organizer.is_verified,
            verification_requested=membership.verification_requested,
            role=MemberRole(membership.role),
        )
        for membership in rows
    ]


def _membership(account: "_User", organizer_id: int) -> OrganizerMembershipOut:
    found = _memberships(account, organizer_id)
    if not found:
        # The membership went meanwhile (a concurrent removal).
        raise Http404
    return found[0]


def _mfa_admin(request: HttpRequest) -> "_User":
    """The signed-in Account, or 403 ``mfa_required`` without a method.

    Runs before any database lookup, so the answer is the same for every
    well-formed code (a malformed one is a 404 from ``PublicCodePath``).
    ``account_auth`` already refused a session that did not pass MFA for
    an Account that has a method.
    """
    account = request_account(request)
    if not has_mfa(account):
        raise MFARequired
    return account


def _organizer_id(code: str) -> int:
    organizer = get_by_public_code(Organizer.objects.only("pk"), code)
    return int(organizer.pk)


@organizer_router.post(
    "/organizers",
    response={
        201: OrganizerMembershipOut,
        403: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
    operation_id="create_organizer",
)
def create_organizer_operation(
    request: HttpRequest, payload: OrganizerCreateIn
) -> Status[OrganizerMembershipOut]:
    """Create an Organizer, with you as its only admin.

    Your login address must be verified (403 otherwise). The Organizer
    starts unverified: it cannot record counted matches or manage Events
    until an OPEN moderator verifies it (ask with
    `request_organizer_verification`). 409 when you are already an admin
    of as many Organizers as an Account may create. No MFA is needed to
    create one; every admin action on it needs MFA.
    """
    account = request_account(request)
    try:
        organizer = self_service.create_organizer(actor=account, name=payload.name)
    except self_service.AddressNotVerified as error:
        raise HttpError(403, str(error)) from None
    except self_service.Conflict as error:
        raise HttpError(409, str(error)) from None
    except self_service.NotFound:
        raise Http404 from None
    except DjangoValidationError as error:
        raise model_validation_error(error) from None
    return Status(201, _membership(account, organizer.pk))


@organizer_router.patch(
    "/organizers/{code}",
    response={
        200: OrganizerMembershipOut,
        403: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
    operation_id="update_organizer",
)
def update_organizer(
    request: HttpRequest, code: OrganizerCode, payload: OrganizerUpdateIn
) -> OrganizerMembershipOut:
    """Change the Organizer's name. For an admin member with MFA.

    Anyone else, and an unknown code, gets the same 404. 409 while the
    Organizer is verified: a moderator verified it under its name, so a
    moderator unverifies it first, or staff rename it.
    """
    account = _mfa_admin(request)
    organizer_id = _organizer_id(code)
    try:
        self_service.rename_organizer(organizer_id, actor=account, name=payload.name)
    except self_service.NotFound:
        raise Http404 from None
    except self_service.Conflict as error:
        raise HttpError(409, str(error)) from None
    except DjangoValidationError as error:
        raise model_validation_error(error) from None
    return _membership(account, organizer_id)


@organizer_router.post(
    "/organizers/{code}/invites",
    response={
        202: OrganizerInviteSentOut,
        403: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
    operation_id="invite_organizer_member",
    throttle=OrganizerInviteThrottle("organizer_invite"),
)
def invite_organizer_member(
    request: HttpRequest, code: OrganizerCode, payload: OrganizerInviteIn
) -> Status[OrganizerInviteSentOut]:
    """Invite an email address to act for the Organizer, as admin or
    match manager. For an admin member with MFA.

    The answer is the same whether or not the address has an OPEN
    Account, is already a member, or was invited before: an invite email
    with a link that works once goes to any address that is not a
    member's, replacing an earlier invite to it. The invitee accepts
    while signed in with that address (`accept_organizer_invite`). 409
    when the Organizer has too many open invites. Limited per Account to
    a number of invites a day; past that the answer is 429 with a
    Retry-After header.
    """
    account = _mfa_admin(request)
    require_step_up(request)
    organizer_id = _organizer_id(code)
    try:
        self_service.invite_member(
            organizer_id, actor=account, email=payload.email, role=payload.role
        )
    except self_service.NotFound:
        raise Http404 from None
    except self_service.Conflict as error:
        raise HttpError(409, str(error)) from None
    return Status(202, OrganizerInviteSentOut(message=INVITE_SENT_MESSAGE))


@organizer_router.post(
    "/invites/accept",
    response={200: OrganizerMembershipOut, 404: ErrorResponse, 409: ErrorResponse},
    operation_id="accept_organizer_invite",
)
def accept_organizer_invite(
    request: HttpRequest, payload: OrganizerInviteAcceptIn
) -> OrganizerMembershipOut:
    """Accept an invite to act for an Organizer, with the key from its email.

    Sign in with the invited address, verified, and have a player
    profile. A key that is unknown, malformed, expired, already used, or
    for another address, an unverified address, an Account with no
    player, and anyone else refused get the same 404. 409 when you are
    already a member; the invite is used up then too.
    """
    account = request_account(request)
    try:
        membership = self_service.accept_invite(payload.key, actor=account)
    except self_service.NotFound:
        raise Http404 from None
    except self_service.Conflict as error:
        raise HttpError(409, str(error)) from None
    return _membership(account, membership.organizer_id)


@organizer_router.get(
    "/organizers/{code}/members",
    response={200: list[OrganizerMemberOut], 403: ErrorResponse, 404: ErrorResponse},
    operation_id="list_organizer_members",
)
def list_organizer_members(
    request: HttpRequest, code: OrganizerCode
) -> list[OrganizerMemberOut]:
    """The Organizer's members. For an admin member with MFA.

    Each with its player's OPEN Code and display name, its role, and
    whether it is you; admins first. Never an email.
    """
    account = _mfa_admin(request)
    organizer_id = _organizer_id(code)
    try:
        members = self_service.list_members(organizer_id, actor=account)
    except self_service.NotFound:
        raise Http404 from None
    return [_member_out(member) for member in members]


def _member_out(member: self_service.MemberView) -> OrganizerMemberOut:
    return OrganizerMemberOut(
        player=member.player,
        display_name=member.display_name,
        role=member.role,
        you=member.you,
    )


@organizer_router.patch(
    "/organizers/{code}/members/{player}",
    response={
        200: OrganizerMemberOut,
        403: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
    operation_id="change_organizer_member_role",
)
def change_organizer_member_role(
    request: HttpRequest,
    code: OrganizerCode,
    player: PlayerCode,
    payload: OrganizerMemberRoleIn,
) -> OrganizerMemberOut:
    """Change a member's role. For an admin member with MFA.

    ``player`` is the member's player OPEN Code; one that is not a member
    of this Organizer gets the same 404 as an unknown code. 409 when it
    would leave the Organizer with no admin.
    """
    account = _mfa_admin(request)
    require_step_up(request)
    organizer_id = _organizer_id(code)
    try:
        changed = self_service.change_member_role(
            organizer_id, player, actor=account, role=payload.role
        )
    except self_service.NotFound:
        raise Http404 from None
    except self_service.Conflict as error:
        raise HttpError(409, str(error)) from None
    return _member_out(changed)


@organizer_router.delete(
    "/organizers/{code}/members/{player}",
    response={204: None, 403: ErrorResponse, 404: ErrorResponse, 409: ErrorResponse},
    operation_id="remove_organizer_member",
)
def remove_organizer_member(
    request: HttpRequest, code: OrganizerCode, player: PlayerCode
) -> Status[None]:
    """Remove a member, yourself included. For an admin member with MFA.

    ``player`` is the member's player OPEN Code; one that is not a member
    of this Organizer gets the same 404 as an unknown code. 409 when it
    would leave the Organizer with no admin.
    """
    account = _mfa_admin(request)
    require_step_up(request)
    organizer_id = _organizer_id(code)
    try:
        self_service.remove_member(organizer_id, player, actor=account)
    except self_service.NotFound:
        raise Http404 from None
    except self_service.Conflict as error:
        raise HttpError(409, str(error)) from None
    return Status(204, None)


@organizer_router.post(
    "/organizers/{code}/verification-request",
    response={
        201: OrganizerVerificationRequestOut,
        403: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
    operation_id="request_organizer_verification",
)
def request_organizer_verification(
    request: HttpRequest, code: OrganizerCode, payload: OrganizerVerificationRequestIn
) -> Status[OrganizerVerificationRequestOut]:
    """Ask OPEN moderators to verify the Organizer. For an admin member
    with MFA.

    ``statement`` says what the Organizer runs, for moderators only. A
    moderator decides; nothing is verified automatically. 409 when the
    Organizer is already verified or already has an open request.
    """
    account = _mfa_admin(request)
    require_step_up(request)
    organizer_id = _organizer_id(code)
    try:
        made = self_service.request_verification(
            organizer_id, actor=account, statement=payload.statement
        )
    except self_service.NotFound:
        raise Http404 from None
    except self_service.Conflict as error:
        raise HttpError(409, str(error)) from None
    return Status(
        201,
        OrganizerVerificationRequestOut(organizer=code, created_at=made.created_at),
    )


@organizer_router.get(
    "/organizers/{code}/club-grants",
    response={200: list[OrganizerClubGrantOut], 403: ErrorResponse, 404: ErrorResponse},
    operation_id="list_organizer_club_grants",
)
def list_organizer_club_grants(
    request: HttpRequest, code: OrganizerCode
) -> list[OrganizerClubGrantOut]:
    """The Clubs that allow the Organizer to name them on its Events and
    matches. For an admin member with MFA.

    Each with its OPEN Code, name, city, region, and when it allowed the
    Organizer, ordered by Club code. A Club decides this; the Organizer
    cannot add or remove one.
    """
    account = _mfa_admin(request)
    organizer_id = _organizer_id(code)
    try:
        grants = self_service.list_club_grants(organizer_id, actor=account)
    except self_service.NotFound:
        raise Http404 from None
    return [
        OrganizerClubGrantOut(
            club=grant.club,
            name=grant.name,
            city=grant.city,
            region=grant.region,
            created_at=grant.created_at,
        )
        for grant in grants
    ]

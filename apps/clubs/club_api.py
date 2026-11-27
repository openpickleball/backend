"""Club self-service at /api/v1/clubs (#230), through
``apps.clubs.club_self_service``.

- ``POST /clubs`` (``create_club``): an Account whose login address is
  verified (403 otherwise) creates a Club and is its only admin; 409
  past the per-Account limit. No MFA is needed.
- ``GET /clubs/{code}`` (``get_club``): any signed-in Account reads a
  Club's public profile (OPEN Code, name, city, region).
- The operations with a Club code in the path that change the Club or
  list its admin data act for a Club admin: the profile change, admin
  invites, the admin list and removal, and the Club Organizer Grants.
  An Account without MFA gets 403 ``mfa_required`` before any database
  lookup, the same for every well-formed code (ADR 0043); a malformed
  code is already a 404 when ``PublicCodePath`` reads the path. A
  non-admin and an unknown or malformed code (or admin Player code, or
  granted Organizer code) all get the same 404.
- ``POST /clubs/invites/accept`` (``accept_club_invite``): the invited
  Account becomes an admin; every refusal is one 404.

A Club Role and a Club Organizer Grant give no authority to submit,
confirm, or change a match (ADR 0021): a grant is consent for a verified
Organizer to name the Club. The invite has its own throttle,
``ClubInviteThrottle``, which replaces the router's Account throttle,
since each invite sends an email.
"""

from typing import Annotated

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404, HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.clubs import club_self_service
from apps.clubs.api import INVITE_SENT_MESSAGE, _mfa_admin
from apps.clubs.models import Club
from apps.clubs.schemas import (
    ClubAdminOut,
    ClubCreateIn,
    ClubInviteAcceptIn,
    ClubInviteIn,
    ClubInviteSentOut,
    ClubOrganizerGrantIn,
    ClubOrganizerGrantOut,
    ClubOut,
    ClubUpdateIn,
)
from apps.clubs.self_service import AddressNotVerified, Conflict, NotFound
from apps.matches.api import request_account
from apps.public_ids.api import PublicCodePath, get_by_public_code
from apps.public_ids.codes import CodeType
from config.api_errors import ErrorResponse, model_validation_error
from config.auth import account_auth
from config.throttling import (
    DAY,
    FixedWindowThrottle,
    Windows,
    account_bucket,
    account_throttle,
)

club_router = Router(tags=["clubs"], auth=account_auth, throttle=account_throttle)

ClubCode = Annotated[str, PublicCodePath(CodeType.CLUB)]
PlayerCode = Annotated[str, PublicCodePath(CodeType.PLAYER)]
OrganizerCode = Annotated[str, PublicCodePath(CodeType.ORGANIZER)]


class ClubInviteThrottle(FixedWindowThrottle):
    """Per-Account Club invites per day, from
    ``API_CLUB_INVITE_THROTTLE_PER_DAY``.

    One day window, counted by the Account's primary key, never its
    email. The rate is read on each request.
    """

    def bucket(self, request: HttpRequest) -> str | None:
        return account_bucket(request)

    def limits(self) -> Windows:
        return (("day", settings.API_CLUB_INVITE_THROTTLE_PER_DAY, DAY),)


def _club_out(club_id: int) -> ClubOut:
    club = Club.objects.filter(pk=club_id).first()
    if club is None:
        # The Club went meanwhile (a concurrent delete).
        raise Http404
    return ClubOut(
        club=club.public_code, name=club.name, city=club.city, region=club.region
    )


def _club_id(code: str) -> int:
    club = get_by_public_code(Club.objects.only("pk"), code)
    return int(club.pk)


@club_router.post(
    "",
    response={201: ClubOut, 403: ErrorResponse, 404: ErrorResponse, 409: ErrorResponse},
    operation_id="create_club",
)
def create_club(request: HttpRequest, payload: ClubCreateIn) -> Status[ClubOut]:
    """Create a Club, with you as its only admin.

    Your login address must be verified (403 otherwise). 409 when you are
    already an admin of as many Clubs as an Account may create. No MFA is
    needed to create one; every admin action on it needs MFA. A Club
    gives no authority to record matches: allow a verified Organizer to
    name it with `grant_club_organizer`.
    """
    account = request_account(request)
    try:
        club = club_self_service.create_club(
            actor=account, name=payload.name, city=payload.city, region=payload.region
        )
    except AddressNotVerified as error:
        raise HttpError(403, str(error)) from None
    except Conflict as error:
        raise HttpError(409, str(error)) from None
    except NotFound:
        raise Http404 from None
    except DjangoValidationError as error:
        raise model_validation_error(error) from None
    return Status(201, _club_out(club.pk))


@club_router.post(
    "/invites/accept",
    response={200: ClubOut, 404: ErrorResponse, 409: ErrorResponse},
    operation_id="accept_club_invite",
)
def accept_club_invite(request: HttpRequest, payload: ClubInviteAcceptIn) -> ClubOut:
    """Accept an invite to be a Club admin, with the key from its email.

    Sign in with the invited address, verified, and have a player
    profile. A key that is unknown, malformed, expired, already used, or
    for another address, an unverified address, an Account with no
    player, and anyone else refused get the same 404. 409 when you are
    already an admin; the invite is used up then too.
    """
    account = request_account(request)
    try:
        role = club_self_service.accept_club_invite(payload.key, actor=account)
    except NotFound:
        raise Http404 from None
    except Conflict as error:
        raise HttpError(409, str(error)) from None
    return _club_out(role.club_id)


@club_router.get(
    "/{code}",
    response={200: ClubOut, 404: ErrorResponse},
    operation_id="get_club",
)
def get_club(request: HttpRequest, code: ClubCode) -> ClubOut:
    """A Club's public profile: its OPEN Code, name, city, and region.

    For any signed-in Account. An unknown code gets 404.
    """
    return _club_out(_club_id(code))


@club_router.patch(
    "/{code}",
    response={200: ClubOut, 403: ErrorResponse, 404: ErrorResponse, 409: ErrorResponse},
    operation_id="update_club",
)
def update_club(request: HttpRequest, code: ClubCode, payload: ClubUpdateIn) -> ClubOut:
    """Change the Club's name, city, or region. For a Club admin with MFA.

    A field left out or null is not changed. Anyone else, and an unknown
    code, gets the same 404. 409 for a new name once any Event or
    recorded match names the Club: players see the name on those
    matches, so OPEN staff rename it. The city and region may still
    change.
    """
    account = _mfa_admin(request)
    club_id = _club_id(code)
    try:
        club_self_service.update_club(
            club_id,
            actor=account,
            name=payload.name,
            city=payload.city,
            region=payload.region,
        )
    except NotFound:
        raise Http404 from None
    except Conflict as error:
        raise HttpError(409, str(error)) from None
    except DjangoValidationError as error:
        raise model_validation_error(error) from None
    return _club_out(club_id)


@club_router.post(
    "/{code}/invites",
    response={
        202: ClubInviteSentOut,
        403: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
    operation_id="invite_club_admin",
    throttle=ClubInviteThrottle("club_invite"),
)
def invite_club_admin(
    request: HttpRequest, code: ClubCode, payload: ClubInviteIn
) -> Status[ClubInviteSentOut]:
    """Invite an email address to be an admin of the Club. For a Club
    admin with MFA.

    The answer is the same whether or not the address has an OPEN
    Account, is already an admin, or was invited before: an invite email
    with a link that works once goes to any address that is not an
    admin's, replacing an earlier invite to it. The invitee accepts
    while signed in with that address (`accept_club_invite`). 409 when
    the Club has too many open invites. Limited per Account to a number
    of invites a day; past that the answer is 429 with a Retry-After
    header.
    """
    account = _mfa_admin(request)
    club_id = _club_id(code)
    try:
        club_self_service.invite_admin(club_id, actor=account, email=payload.email)
    except NotFound:
        raise Http404 from None
    except Conflict as error:
        raise HttpError(409, str(error)) from None
    return Status(202, ClubInviteSentOut(message=INVITE_SENT_MESSAGE))


@club_router.get(
    "/{code}/admins",
    response={200: list[ClubAdminOut], 403: ErrorResponse, 404: ErrorResponse},
    operation_id="list_club_admins",
)
def list_club_admins(request: HttpRequest, code: ClubCode) -> list[ClubAdminOut]:
    """The Club's admins. For a Club admin with MFA.

    Each with its player's OPEN Code and display name, and whether it is
    you. Never an email.
    """
    account = _mfa_admin(request)
    club_id = _club_id(code)
    try:
        admins = club_self_service.list_admins(club_id, actor=account)
    except NotFound:
        raise Http404 from None
    return [
        ClubAdminOut(
            player=admin.player, display_name=admin.display_name, you=admin.you
        )
        for admin in admins
    ]


@club_router.delete(
    "/{code}/admins/{player}",
    response={204: None, 403: ErrorResponse, 404: ErrorResponse, 409: ErrorResponse},
    operation_id="remove_club_admin",
)
def remove_club_admin(
    request: HttpRequest, code: ClubCode, player: PlayerCode
) -> Status[None]:
    """Remove an admin, yourself included. For a Club admin with MFA.

    ``player`` is the admin's player OPEN Code; one that is not an admin
    of this Club gets the same 404 as an unknown code. 409 when it would
    leave the Club with no admin. The invites that admin sent to this
    Club stop working.
    """
    account = _mfa_admin(request)
    club_id = _club_id(code)
    try:
        club_self_service.remove_admin(club_id, player, actor=account)
    except NotFound:
        raise Http404 from None
    except Conflict as error:
        raise HttpError(409, str(error)) from None
    return Status(204, None)


@club_router.get(
    "/{code}/organizer-grants",
    response={
        200: list[ClubOrganizerGrantOut],
        403: ErrorResponse,
        404: ErrorResponse,
    },
    operation_id="list_club_organizer_grants",
)
def list_club_organizer_grants(
    request: HttpRequest, code: ClubCode
) -> list[ClubOrganizerGrantOut]:
    """The Organizers the Club allows to name it on Events and matches.
    For a Club admin with MFA.

    Each by its OPEN Code, with when it was allowed, ordered by code.
    Never an Organizer's name.
    """
    account = _mfa_admin(request)
    club_id = _club_id(code)
    try:
        grants = club_self_service.list_grants(club_id, actor=account)
    except NotFound:
        raise Http404 from None
    return [
        ClubOrganizerGrantOut(organizer=grant.organizer, created_at=grant.created_at)
        for grant in grants
    ]


@club_router.post(
    "/{code}/organizer-grants",
    response={
        201: ClubOrganizerGrantOut,
        403: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
    },
    operation_id="grant_club_organizer",
)
def grant_club_organizer(
    request: HttpRequest, code: ClubCode, payload: ClubOrganizerGrantIn
) -> Status[ClubOrganizerGrantOut]:
    """Allow an Organizer to name the Club on its Events and matches. For
    a Club admin with MFA.

    The grant is consent, not trust: only a verified Organizer's matches
    count, and this changes nothing about verification. An unknown or
    malformed Organizer code gets the same 404 as an unknown Club. 409
    when the Club already allows it.
    """
    account = _mfa_admin(request)
    club_id = _club_id(code)
    try:
        grant = club_self_service.grant_organizer(
            club_id, payload.organizer, actor=account
        )
    except NotFound:
        raise Http404 from None
    except Conflict as error:
        raise HttpError(409, str(error)) from None
    return Status(
        201,
        ClubOrganizerGrantOut(
            organizer=grant.organizer.public_code, created_at=grant.created_at
        ),
    )


@club_router.delete(
    "/{code}/organizer-grants/{organizer}",
    response={204: None, 403: ErrorResponse, 404: ErrorResponse},
    operation_id="remove_club_organizer_grant",
)
def remove_club_organizer_grant(
    request: HttpRequest, code: ClubCode, organizer: OrganizerCode
) -> Status[None]:
    """Stop allowing an Organizer to name the Club. For a Club admin with
    MFA.

    Events and matches already recorded keep the Club; a new Event or
    match naming it is refused from then on. An Organizer the Club does
    not allow gets the same 404 as an unknown code.
    """
    account = _mfa_admin(request)
    club_id = _club_id(code)
    try:
        club_self_service.remove_grant(club_id, organizer, actor=account)
    except NotFound:
        raise Http404 from None
    return Status(204, None)

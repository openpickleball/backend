"""First-party match endpoints at /api/v1/matches/ (issue #31).

A signed-in player submits a match they played in. With no Event and no
Club it is a self-submission (decision F1): it is created pending, and
becomes confirmed once a player on each team confirms it. The submission
counts as the submitter's own confirmation (issue #197): ``create_match``
calls ``workflow.confirm_match`` for the submitter in the transaction that
records the match, so the other team's confirmation is the one it still
needs. One team alone never confirms a match. Score
confirmation is a security-sensitive area (CLAUDE.md, hard rule 8), so
every rule lives in the services these views call: ``record_match`` and
``apps.matches.workflow``. The views only translate requests.

Submission refuses an Account whose own Player is not on either team
before it looks up any code or validates anything, so a refused submitter
learns nothing about which codes exist. ``record_match`` checks again.

One match is at /api/v1/matches/{code}, for the players who played in it
only (``permissions.participant_player``, the rule confirm and dispute
use). Anyone else, an organizer of its Event included, gets the same 404
as an unknown or malformed code, so the response never reveals that the
match exists (ADR 0008).

Confirm and dispute answer an actor the workflow refuses
(``MatchActionDenied``) with the same 404 as an unknown or malformed
code, so the response never reveals that the match exists (ADR 0008).
A participant whose change the match's status does not allow still gets
409.

The matches awaiting the signed-in player's confirmation are at
/api/v1/me/matches/awaiting-confirmation (``me_router``): pending matches
the player played in that nobody on their team has confirmed yet
(``workflow.awaiting_confirmation``). It reads only the signed-in
Account's own Player, so it never shows another player's matches. An
Account with no Player gets an empty page.

Each service runs in its own transaction, never inside another one: their
advisory locks last until the outermost transaction ends. The one
exception is ``create_match``, which confirms the match it just recorded
in the same transaction. That is one ``record_match`` call per
transaction, and ``confirm_match`` takes the lineup lock ``record_match``
already holds, then the row lock of a match no other transaction can see
yet, so the lock order stays the same and nothing can wait on it.

No rule reads payment status: every player may submit and confirm every
match (hard rule 7).
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, cast

from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404, HttpRequest
from ninja import Router, Status
from ninja.pagination import paginate

from apps.matches import workflow
from apps.matches.models import Match
from apps.matches.pagination import MatchHistoryPagination
from apps.matches.permissions import (
    MatchActionDenied,
    MatchPermissionDenied,
    participant_player,
)
from apps.matches.schemas import MatchCreateIn, MatchOut, match_out, with_match_details
from apps.matches.services import MatchIssue, MatchValidationError, record_match
from apps.players.models import Player
from apps.public_ids.api import PublicCodePath, get_by_public_code
from apps.public_ids.codes import CodeType, normalize
from config.api_errors import ErrorResponse
from config.auth import account_auth
from config.throttling import account_throttle

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

router = Router(tags=["matches"], auth=account_auth, throttle=account_throttle)
# The signed-in Account's own matches, mounted at /api/v1/me/.
me_router = Router(tags=["matches"], auth=account_auth, throttle=account_throttle)

MatchCode = Annotated[str, PublicCodePath(CodeType.MATCH)]

UNKNOWN_PLAYER_MESSAGE = "No player has this OPEN Code."

_BODY_FIELDS = frozenset(MatchCreateIn.model_fields)
_TEAMS = ("team_a", "team_b")


def request_account(request: HttpRequest) -> "_User":
    """The signed-in Account. ``account_auth`` refuses everyone else, and
    puts the Account itself in ``request.auth`` (``config/auth.py``)."""
    return cast("_User", request.auth)  # type: ignore[attr-defined]


def _in_body(error: MatchValidationError) -> MatchValidationError:
    """The error with each request field's issue located in the body.

    Pydantic's own errors for this body are ``body.<field>``, so the
    service's issues for the same fields get the same prefix (ADR 0005).
    """
    return MatchValidationError(
        [
            MatchIssue(f"body.{issue.field}", issue.code, issue.message)
            if issue.field.split(".", 1)[0] in _BODY_FIELDS
            else issue
            for issue in error.issues
        ]
    )


def _check_submitter_plays(account: "_User", payload: MatchCreateIn) -> None:
    """Refuse an Account whose own Player is on neither team.

    Runs before any code lookup or validation. A malformed code never
    matches, so it cannot pass this check.
    """
    own_code = (
        Player.objects.filter(account=account)
        .values_list("public_code", flat=True)
        .first()
    )
    submitted = {
        normalize(raw, CodeType.PLAYER)
        for team in _TEAMS
        for raw in getattr(payload, team)
    }
    if own_code is None or own_code not in submitted:
        raise MatchPermissionDenied


def _resolve_teams(payload: MatchCreateIn) -> dict[str, list[Player]]:
    """Each team's Players, in one query.

    A malformed code and a code no Player has get the same issue, with
    fixed text: the message never repeats what was sent.
    """
    codes = {
        team: [normalize(raw, CodeType.PLAYER) for raw in getattr(payload, team)]
        for team in _TEAMS
    }
    wanted = {code for team in codes.values() for code in team if code is not None}
    found = {
        player.public_code: player
        for player in Player.objects.filter(public_code__in=wanted)
    }
    issues = [
        MatchIssue(f"body.{team}.{index}", "unknown_player", UNKNOWN_PLAYER_MESSAGE)
        for team in _TEAMS
        for index, code in enumerate(codes[team])
        if code is None or code not in found
    ]
    if issues:
        raise MatchValidationError(issues)
    return {
        team: [found[code] for code in codes[team] if code is not None]
        for team in _TEAMS
    }


def _loaded(match: Match) -> Match:
    return with_match_details(Match.objects.filter(pk=match.pk)).get()


_SUBMIT_RESPONSES = {201: MatchOut, 403: ErrorResponse, 409: ErrorResponse}


@router.post("/", response=_SUBMIT_RESPONSES, operation_id="create_match")
def create_match(request: HttpRequest, payload: MatchCreateIn) -> Status[MatchOut]:
    """Submit a match you played in. It counts as your team's confirmation.

    The match stays pending until a player on the other team confirms it.
    Players are given by OPEN Code. A missing game target means 11, a
    missing scoring type means side-out, and a missing best-of is inferred
    from the games.
    """
    account = request_account(request)
    _check_submitter_plays(account, payload)
    teams = _resolve_teams(payload)
    try:
        with transaction.atomic():
            recorded = record_match(
                format=payload.format,
                team_a=teams["team_a"],
                team_b=teams["team_b"],
                games=[(game.team_a, game.team_b) for game in payload.games],
                played_on=payload.played_on,
                best_of=payload.best_of,
                game_target=payload.game_target,
                scoring_type=payload.scoring_type,
                submitted_by_user=account,
            )
            # The submission is the submitter's own confirmation (#197). A
            # self-submission is always created pending, and the submitter
            # is a participant, so this adds their team's confirmation
            # only: the other team's is still needed. The guard keeps a
            # future change from adding a confirmation to an existing match.
            if recorded.created:
                workflow.confirm_match(recorded.match, actor=account)
    except MatchValidationError as error:
        raise _in_body(error) from None
    return Status(201, match_out(_loaded(recorded.match), account))


@router.get(
    "/{code}",
    response={200: MatchOut, 404: ErrorResponse},
    operation_id="get_match",
)
def get_match(request: HttpRequest, code: MatchCode) -> MatchOut:
    """One match you played in, by its OPEN Code, in any status.

    Anyone who did not play in the match gets 404, as for an unknown code.
    """
    account = request_account(request)
    match = get_by_public_code(Match.objects.all(), code)
    if participant_player(account, match) is None:
        raise Http404
    return match_out(_loaded(match), account)


_CHANGE_RESPONSES = {
    200: MatchOut,
    403: ErrorResponse,
    404: ErrorResponse,
    409: ErrorResponse,
}
# The only 403 these operations return is a session request's CSRF
# failure (``config/auth.py``). A refused actor gets the 404.
_CHANGE_OPENAPI = {
    "responses": {
        403: {
            "description": (
                "Only a signed-in browser session request without a valid "
                "CSRF token. A caller who may not change this match gets "
                "404, as for an unknown code."
            )
        }
    }
}


def _change(
    request: HttpRequest,
    code: str,
    service: Callable[..., Match],
) -> MatchOut:
    """Run a workflow change as the signed-in Account and return the match.

    A refused actor gets the unknown-code 404, never a 403.
    """
    account = request_account(request)
    match = get_by_public_code(Match.objects.all(), code)
    try:
        with transaction.atomic():
            service(match, actor=account)
    except MatchActionDenied:
        raise Http404 from None
    return match_out(_loaded(match), account)


@router.post(
    "/{code}/confirm",
    response=_CHANGE_RESPONSES,
    operation_id="confirm_match",
    openapi_extra=_CHANGE_OPENAPI,
)
def confirm_match(request: HttpRequest, code: MatchCode) -> MatchOut:
    """Confirm a match you played in.

    A pending match becomes confirmed once a player on each team confirms
    it. Confirming again changes nothing. A disputed or voided match
    cannot be confirmed (409). Anyone who did not play in the match gets
    404, as for an unknown code.
    """
    return _change(request, code, workflow.confirm_match)


@router.post(
    "/{code}/dispute",
    response=_CHANGE_RESPONSES,
    operation_id="dispute_match",
    openapi_extra=_CHANGE_OPENAPI,
)
def dispute_match(request: HttpRequest, code: MatchCode) -> MatchOut:
    """Dispute a match you played in. A moderator then reviews it.

    Disputing again changes nothing. A voided match cannot be disputed
    (409). Anyone who did not play in the match gets 404, as for an
    unknown code.
    """
    return _change(request, code, workflow.dispute_match)


@me_router.get(
    "/matches/awaiting-confirmation",
    response={200: list[MatchOut]},
    operation_id="list_matches_awaiting_confirmation",
)
@paginate(MatchHistoryPagination)
def list_matches_awaiting_confirmation(request: HttpRequest) -> QuerySet[Match]:
    """The pending matches waiting for your confirmation, newest first.

    A match is listed while it is pending, you played in it, and nobody on
    your team has confirmed it yet. Once you or a teammate confirms it,
    your team is done and it leaves the list. A match you submitted is not
    listed, since submitting confirms it for your team.
    """
    viewer = request_account(request)
    player = Player.objects.filter(account=viewer).first()
    if player is None:
        return Match.objects.none()
    return with_match_details(workflow.awaiting_confirmation(player)).order_by(
        "-played_on", "-created_at", "-pk"
    )

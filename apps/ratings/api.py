"""Rating endpoints at /api/v1/players/ (issue #53).

Both operations need sign-in (``account_auth``) and share the per-Account
throttle of every signed-in operation (``account_throttle``, ADR 0030).

The rating lookup (``get_player_ratings``): any signed-in Account sees the
current Ratings of every player whose Account is active, players aged 13
to 17 included. Current Ratings have no privacy setting (ADR 0002, ADR
0009), so none is read, and the answer never changes when a player turns
18, so it reveals no age. A player whose Account is inactive gets the
same 404 as an unknown or malformed code, so the answer never reveals
that such a player exists (ADR 0008).

A player's rating history (``list_player_rating_history``) follows the
player's ``rating_history_visibility`` exactly as match history follows
``match_history_visibility`` (``PlayerPrivacy.visible_to``): the player's
own Account always sees it; another Account sees it only at ``signed_in``
and only while the player's Account is active. A viewer it is hidden from
gets the same 404 as an unknown or malformed code.

Every rating comes with its reliability and ``demonstration: true``
(``apps.ratings.schemas``).
"""

from typing import Annotated

from django.conf import settings
from django.db.models import QuerySet
from django.http import Http404, HttpRequest
from ninja import Router
from ninja.pagination import paginate

from apps.matches.api import request_account
from apps.matches.models import RatingFormat
from apps.players.models import Player
from apps.public_ids.api import PublicCodePath, get_by_public_code
from apps.public_ids.codes import CodeType
from apps.ratings.models import RatingHistory, RatingState
from apps.ratings.pagination import RatingHistoryPagination
from apps.ratings.schemas import (
    DEMONSTRATION_RATINGS,
    PlayerRatingsOut,
    RatingHistoryItemOut,
    rating_out,
)
from config.api_errors import ErrorResponse
from config.auth import account_auth
from config.throttling import account_throttle

router = Router(tags=["ratings"], auth=account_auth, throttle=account_throttle)

PlayerCode = Annotated[str, PublicCodePath(CodeType.PLAYER)]


@router.get(
    "/{code}/ratings",
    response={200: PlayerRatingsOut, 404: ErrorResponse},
    operation_id="get_player_ratings",
)
def get_player_ratings(request: HttpRequest, code: PlayerCode) -> PlayerRatingsOut:
    """A player's current Ratings, one per rating format the player has.

    Any signed-in Account sees them for every player whose Account is
    active, players aged 13 to 17 included: current Ratings have no
    privacy setting. Each rating comes with its reliability: the OPEN
    standard requires any app that shows an OPEN rating to show its
    reliability next to it. Every rating is a demonstration rating while
    the engine is a demonstration package. An unknown or malformed code and
    a player whose Account is inactive get the same 404.
    """
    player = get_by_public_code(Player.objects.select_related("account"), code)
    if not player.account.is_active:
        raise Http404
    states = RatingState.objects.filter(player=player).order_by("rating_format")
    return PlayerRatingsOut(
        code=player.public_code,
        profile_url=f"{settings.OPEN_PUBLIC_PROFILE_URL}{player.public_code}",
        demonstration=DEMONSTRATION_RATINGS,
        ratings={
            RatingFormat(state.rating_format): rating_out(state) for state in states
        },
    )


@router.get(
    "/{code}/rating-history",
    response={200: list[RatingHistoryItemOut], 404: ErrorResponse},
    operation_id="list_player_rating_history",
)
@paginate(RatingHistoryPagination)
def list_player_rating_history(
    request: HttpRequest, code: PlayerCode
) -> QuerySet[RatingHistory]:
    """Every change to a player's ratings, in every rating format, newest
    first.

    Each change comes with its reliability: the OPEN standard requires any
    app that shows an OPEN rating to show its reliability next to it.
    Every rating is a demonstration rating while the engine is a
    demonstration package. The player always sees their own history.
    Anyone else signed in sees it only when the player's rating history
    setting is signed-in players, and gets 404, as for an unknown code,
    when it is private or the player's Account is inactive.
    """
    viewer = request_account(request)
    player = get_by_public_code(
        Player.objects.select_related("account", "privacy"), code
    )
    if not player.privacy.visible_to("rating_history_visibility", viewer):
        raise Http404
    return RatingHistory.objects.filter(player=player).order_by("-created_at", "-id")

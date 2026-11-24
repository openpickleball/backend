"""Rating endpoints at /api/v1/players/ (issue #53).

The public rating lookup (``get_player_ratings``) needs no sign-in: anyone
may read the current Ratings of a player whose Account is active and who
is 18 or older today (``is_publicly_rated``, the ADR 0017 rule).
Current Ratings have no privacy setting (ADR 0002), so none is read.
Anyone else gets the same 404 as an unknown or malformed code, so the
answer never reveals that a player exists (ADR 0008). The view never
reads the session, the signed-in user, or a CSRF token, so it never sets
a cookie, and a signed-in request gets the same answer as an anonymous
one. Its only throttle is per client IP (``ClientIPThrottle``, ADR 0030).

A player's rating history (``list_player_rating_history``) needs sign-in
and follows the player's ``rating_history_visibility`` exactly as match
history follows ``match_history_visibility`` (``PlayerPrivacy.visible_to``):
the player's own Account always sees it; another Account sees it only at
``signed_in`` and only while the player's Account is active. The setting's
most open level is signed-in players (ADR 0002), so it is never public. A
viewer it is hidden from gets the same 404 as an unknown or malformed
code. It shares the per-Account throttle of every signed-in operation.

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
from apps.players.age import is_adult
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
from config.throttling import ClientIPThrottle, account_throttle

router = Router(tags=["ratings"])

PlayerCode = Annotated[str, PublicCodePath(CodeType.PLAYER)]


def is_publicly_rated(player: Player) -> bool:
    """Whether anyone may read ``player``'s current Ratings.

    Only while the player's Account is active and the player is 18 or
    older on the age limit date at the time of the request (``is_adult``,
    ADR 0017). Fails closed: a missing birth year or month, or one that
    makes the player under 13 or born in the future (a row written past
    the model's checks), is never public.
    """
    year: int | None = player.birth_year
    month: int | None = player.birth_month
    if year is None or month is None:
        return False
    return bool(player.account.is_active) and is_adult(year, month)


@router.get(
    "/{code}/ratings",
    response={200: PlayerRatingsOut, 404: ErrorResponse},
    operation_id="get_player_ratings",
    auth=None,
    throttle=ClientIPThrottle("player_ratings"),
)
def get_player_ratings(request: HttpRequest, code: PlayerCode) -> PlayerRatingsOut:
    """A player's current Ratings, one per rating format the player has.

    No sign-in needed. Each rating comes with its reliability: the OPEN
    standard requires any app that shows an OPEN rating to show its
    reliability next to it. Every rating is a demonstration rating while
    the engine is a demonstration package. An unknown code, a player whose
    Account is inactive, and a player aged 13 to 17 get the same 404.
    Limited per client IP; past the limit the answer is 429 with a
    Retry-After header.
    """
    player = get_by_public_code(Player.objects.select_related("account"), code)
    if not is_publicly_rated(player):
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
    auth=account_auth,
    throttle=account_throttle,
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

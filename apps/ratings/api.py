"""Rating endpoints at /api/v1/players/ (issue #53).

The public rating lookup (``get_player_ratings``) needs no sign-in: anyone
may read the current Ratings of a player whose Account is active and who
is not aged 13 to 17 today (``is_publicly_rated``, the ADR 0017 rule).
Current Ratings have no privacy setting (ADR 0002), so none is read.
Anyone else gets the same 404 as an unknown or malformed code, so the
answer never reveals that a player exists (ADR 0008). The view never
reads the session, the signed-in user, or a CSRF token, so it never sets
a cookie, and a signed-in request gets the same answer as an anonymous
one. Its only throttle is per client IP (``ClientIPThrottle``, ADR 0030).

Every rating comes with its reliability and ``demonstration: true``
(``apps.ratings.schemas``).
"""

from typing import Annotated

from django.conf import settings
from django.http import Http404, HttpRequest
from ninja import Router

from apps.matches.models import RatingFormat
from apps.players.age import is_minor
from apps.players.models import Player
from apps.public_ids.api import PublicCodePath, get_by_public_code
from apps.public_ids.codes import CodeType
from apps.ratings.models import RatingState
from apps.ratings.schemas import (
    DEMONSTRATION_RATINGS,
    PlayerRatingsOut,
    rating_out,
)
from config.api_errors import ErrorResponse
from config.throttling import ClientIPThrottle

router = Router(tags=["ratings"])

PlayerCode = Annotated[str, PublicCodePath(CodeType.PLAYER)]


def is_publicly_rated(player: Player) -> bool:
    """Whether anyone may read ``player``'s current Ratings.

    Only while the player's Account is active and the player is not aged
    13 to 17 today, judged by ``is_minor`` at the time of the request.
    """
    return bool(player.account.is_active) and not is_minor(
        player.birth_year, player.birth_month
    )


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

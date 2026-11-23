"""Player endpoints at /api/v1/players/ (issue #31).

A player's match history: the player's own Account sees every match they
played, in every status. Another signed-in Account sees only confirmed
matches, and only while the player's ``match_history_visibility`` lets it
(``PlayerPrivacy.visible_to``, which also hides the history while the
player's Account is inactive). A viewer it hides the history from gets
the same 404 as an unknown or malformed code, so the response never
reveals that the player exists (ADR 0008). The status filter is in the query, so the
page's ``count`` never counts a match the viewer may not see.

Player search (issue #191): a signed-in Account finds players to pick as
opponents or partners by the start of a word in their display name (the
name starts with ``q``, or a word after a space or a hyphen does), case
insensitive. It returns only each player's OPEN Code and display name,
and never a player whose Account is inactive. ADR 0002 shows a Display
Name to every signed-in Account once Ratings are published; ADR 0018
allows this search before then. A query of at least three characters, at
most ``SEARCH_MAX_LIMIT`` results, and a per-Account throttle
(``SearchThrottle``) keep it from listing every player. ninja checks
throttles after auth and before it parses the query, so a refused query
counts toward the limit and an unauthenticated request does not.

A player's profile (issue #124): any signed-in Account sees a player's
OPEN Code and display name, what search already shows, and their city
and region while the player's ``city_region_visibility`` lets the viewer.
A player whose Account is inactive gets the same 404 as an unknown or
malformed code (ADR 0008, ADR 0015).

The signed-in Account's own Player is at /api/v1/me/player (``me_router``,
issue #124). POST creates it, once per Account, only for an Account whose
own login address is verified. The birth year and month come from the
age the signup held in the session (ADR 0017, ``auth_session`` for either
headless client), and from the body only when the session holds none.
"""

import threading
import time
import unicodedata
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, ClassVar

from allauth.account.models import EmailAddress
from django.core.cache import cache as default_cache
from django.core.exceptions import NON_FIELD_ERRORS
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Case, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Lower
from django.http import Http404, HttpRequest
from ninja import Field, Query, Router, Schema, Status
from ninja.errors import HttpError
from ninja.errors import ValidationError as NinjaValidationError
from ninja.pagination import paginate
from ninja.throttling import BaseThrottle
from pydantic import BeforeValidator
from pydantic_core import PydanticCustomError

from apps.matches.api import request_account
from apps.matches.models import Match, MatchStatus
from apps.matches.pagination import (
    MatchHistoryPagination,
)
from apps.matches.schemas import MatchOut, with_match_details
from apps.players.age import peek_signup_birth, take_signup_birth
from apps.players.models import Player
from apps.players.schemas import (
    MyPlayerOut,
    PlayerCreateIn,
    PlayerProfileOut,
    my_player_out,
    player_profile_out,
)
from apps.players.services import create_player
from apps.public_ids.api import PublicCodePath, get_by_public_code
from apps.public_ids.codes import CodeType
from config.api_errors import ErrorResponse
from config.auth import account_auth, auth_session

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

router = Router(tags=["players"], auth=account_auth)
# The signed-in Account's own Player, mounted at /api/v1/me/.
me_router = Router(tags=["players"], auth=account_auth)

PlayerCode = Annotated[str, PublicCodePath(CodeType.PLAYER)]

SEARCH_MIN_LENGTH = 3
SEARCH_MAX_LENGTH = 64
SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 20
SEARCH_MINUTE_LIMIT = 30
SEARCH_DAY_LIMIT = 300
MINUTE = 60
DAY = 24 * 60 * 60
# Extra seconds a counter outlives its window, so it is never dropped
# while its window is still open.
THROTTLE_TIMEOUT_MARGIN = 60
# Fixed text, never a submitted value.
EMAIL_NOT_VERIFIED_MESSAGE = "Verify your email address before creating your player."
ALREADY_HAS_PLAYER_MESSAGE = "This account already has a player."
_MISSING_BIRTH_MESSAGES = {
    "birth_year": "Enter your birth year.",
    "birth_month": "Enter your birth month.",
}
# Control characters and lone surrogates. Postgres refuses a NUL in a
# query parameter, which would be a 500.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})


@router.get(
    "/{code}/matches",
    response={200: list[MatchOut], 404: ErrorResponse},
    operation_id="list_player_matches",
)
@paginate(MatchHistoryPagination)
def list_player_matches(request: HttpRequest, code: PlayerCode) -> QuerySet[Match]:
    """The matches a player played, newest first.

    The player sees every status. Anyone else signed in sees confirmed
    matches only, and gets 404, as for an unknown code, when the player
    keeps their match history private or their Account is inactive.
    """
    viewer = request_account(request)
    player = get_by_public_code(
        Player.objects.select_related("account", "privacy"), code
    )
    matches = Match.objects.filter(participants__player=player)
    if viewer.pk != player.account_id:
        if not player.privacy.visible_to("match_history_visibility", viewer):
            raise Http404
        matches = matches.filter(status=MatchStatus.CONFIRMED)
    return with_match_details(matches).order_by("-played_on", "-created_at", "-pk")


class SearchThrottle(BaseThrottle):
    """Per-Account limits on player search: a minute window and a day window.

    Each window is a fixed window: one counter per scope, Account primary
    key, and window index (``int(now // period)``), counted with
    ``cache.add`` then ``cache.incr``, which are atomic on the Redis and
    local memory caches, so requests from one Account on several workers
    never lose a count. The key never holds ``str(request.auth)``, which is
    the Account's email.

    The minute window is checked first, and the day counter grows only
    when the minute window allows the request, so a burst the minute limit
    refuses does not use up the day. Fixed windows allow a burst across a
    window edge (about 60 searches within seconds across a minute edge,
    about 600 across the UTC day edge). Accepted.

    ninja builds one instance per operation and shares it across requests
    and threads, so nothing about a request is stored on ``self``. What
    ``wait()`` needs (when the refusing window ends) is kept in a
    ``threading.local``: ninja calls ``wait()``, with no request, right
    after ``allow_request`` returns False, on the same thread.
    """

    cache = default_cache
    # A plain function, so tests can replace the clock.
    timer: Callable[[], float] = staticmethod(time.time)
    windows: ClassVar[tuple[tuple[str, int, int], ...]] = (
        ("minute", SEARCH_MINUTE_LIMIT, MINUTE),
        ("day", SEARCH_DAY_LIMIT, DAY),
    )

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self._refused = threading.local()

    def _count(self, key: str, period: int) -> int:
        """Add one to a window's counter and return the new count.

        If the counter expires or is evicted between ``add`` and ``incr``
        (``incr`` raises ``ValueError``), try once more, then allow the
        request (count 0) rather than fail it.
        """
        timeout = period + THROTTLE_TIMEOUT_MARGIN
        for _ in range(2):
            self.cache.add(key, 0, timeout)
            try:
                return int(self.cache.incr(key))
            except ValueError:
                continue
        return 0

    def allow_request(self, request: HttpRequest) -> bool:
        self._refused.retry_after = None
        account_pk = getattr(getattr(request, "auth", None), "pk", None)
        if account_pk is None:
            return True
        now = self.timer()
        for name, limit, period in self.windows:
            window = int(now // period)
            key = f"throttle:{self.scope}:{name}:{account_pk}:{window}"
            if self._count(key, period) > limit:
                self._refused.retry_after = max(1.0, (window + 1) * period - now)
                return False
        return True

    def wait(self) -> float | None:
        retry_after: float | None = getattr(self._refused, "retry_after", None)
        return retry_after


def _search_text(value: object) -> object:
    """Strip a search query and check it, with fixed messages only.

    The messages never repeat the value, since a 422 returns them to the
    client.
    """
    if not isinstance(value, str):
        raise PydanticCustomError("string_type", "Enter the search text.")
    text = value.strip()
    if any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in text):
        raise PydanticCustomError(
            "invalid_characters", "The search text has a character not allowed."
        )
    if len(text) < SEARCH_MIN_LENGTH:
        raise PydanticCustomError(
            "too_short",
            f"Enter at least {SEARCH_MIN_LENGTH} characters to search.",
        )
    if len(text) > SEARCH_MAX_LENGTH:
        raise PydanticCustomError(
            "too_long",
            f"Enter at most {SEARCH_MAX_LENGTH} characters to search.",
        )
    return text


SearchText = Annotated[
    str,
    BeforeValidator(_search_text),
    Field(
        json_schema_extra={
            "minLength": SEARCH_MIN_LENGTH,
            "maxLength": SEARCH_MAX_LENGTH,
        }
    ),
]


class PlayerSearchQuery(Schema):
    """The search text and how many players to return."""

    q: SearchText = Field(
        description=(
            "The start of a word in the display name, "
            f"{SEARCH_MIN_LENGTH} to {SEARCH_MAX_LENGTH} characters "
            "after leading and trailing spaces are removed."
        )
    )
    limit: int = Field(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT)


class PlayerSearchResult(Schema):
    """One player found: their OPEN Code and display name, nothing else."""

    code: str
    display_name: str


class PlayerSearchOut(Schema):
    """The players found, best match first. No count and no paging."""

    items: list[PlayerSearchResult]


def search_players_by_name(text: str, limit: int) -> list[Player]:
    """Players whose display name, or a word in it, starts with ``text``.

    Case insensitive. Django escapes LIKE wildcards, so ``%``, ``_``, and
    ``\\`` match literally. Names that start with ``text`` come first,
    then by name (case insensitive), then by OPEN Code. One query.
    """
    matches = (
        Q(display_name__istartswith=text)
        | Q(display_name__icontains=f" {text}")
        | Q(display_name__icontains=f"-{text}")
    )
    return list(
        Player.objects.filter(account__is_active=True)
        .filter(matches)
        .annotate(
            later_word=Case(
                When(display_name__istartswith=text, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("later_word", Lower("display_name"), "public_code")
        .only("public_code", "display_name")[:limit]
    )


@router.get(
    "/search",
    response={200: PlayerSearchOut, 429: ErrorResponse},
    operation_id="search_players",
    throttle=SearchThrottle("player_search"),
)
def search_players(
    request: HttpRequest, query: Query[PlayerSearchQuery]
) -> PlayerSearchOut:
    """Find players by the start of a word in their display name.

    For picking an opponent or partner. Players whose Account is inactive
    never appear. Limited per Account to 30 searches a minute and 300 a
    day; past that the answer is 429 with a Retry-After header.
    """
    players = search_players_by_name(query.q, query.limit)
    return PlayerSearchOut(
        items=[
            PlayerSearchResult(
                code=player.public_code, display_name=player.display_name
            )
            for player in players
        ]
    )


# Registered after "/search", so that path never reaches this one.
@router.get(
    "/{code}",
    response={200: PlayerProfileOut, 404: ErrorResponse},
    operation_id="get_player",
)
def get_player(request: HttpRequest, code: PlayerCode) -> PlayerProfileOut:
    """A player's OPEN Code and display name, and their city and region
    when the player's privacy settings let the viewer see them.

    A player whose Account is inactive gets the same 404 as an unknown or
    malformed code.
    """
    viewer = request_account(request)
    player = get_by_public_code(
        Player.objects.select_related("account", "privacy"), code
    )
    if not player.account.is_active:
        raise Http404
    return player_profile_out(player, viewer)


def _has_verified_email(account: "_User") -> bool:
    """Whether the Account's own login address is verified.

    Only the row for ``User.email`` counts: a verified old address, or
    another pending one, does not.
    """
    return bool(
        EmailAddress.objects.filter(
            user=account, email=account.email, verified=True
        ).exists()
    )


def _has_player(account: "_User") -> bool:
    return Player.objects.filter(account=account).exists()


def _is_duplicate_player(error: DjangoValidationError) -> bool:
    """Whether ``create_player`` refused a second Player for the Account."""
    found = getattr(error, "error_dict", {}).get("account", [])
    return any(item.code == "unique" for item in found)


def _body_errors(error: DjangoValidationError) -> NinjaValidationError:
    """A model ``ValidationError`` as the API's 422, located in the body.

    The model's messages are fixed text, never a submitted value
    (``apps.players.models``), and only they are copied.
    """
    by_field = (
        error.error_dict
        if hasattr(error, "error_dict")
        else {NON_FIELD_ERRORS: error.error_list}
    )
    items: list[dict[str, object]] = []
    for name, field_errors in by_field.items():
        loc = ("body",) if name == NON_FIELD_ERRORS else ("body", name)
        for item in field_errors:
            items.extend(
                {"loc": loc, "type": item.code or "invalid", "msg": message}
                for message in item
            )
    return NinjaValidationError(items)


def _birth_from_body(payload: PlayerCreateIn) -> tuple[int, int]:
    """The body's birth year and month, or a 422 naming each one missing."""
    missing = [
        {"loc": ("body", name), "type": "missing", "msg": message}
        for name, message in _MISSING_BIRTH_MESSAGES.items()
        if getattr(payload, name) is None
    ]
    if missing or payload.birth_year is None or payload.birth_month is None:
        raise NinjaValidationError(missing)
    return payload.birth_year, payload.birth_month


@me_router.post(
    "/player",
    response={201: MyPlayerOut, 403: ErrorResponse, 409: ErrorResponse},
    operation_id="create_my_player",
)
def create_my_player(
    request: HttpRequest, payload: PlayerCreateIn
) -> Status[MyPlayerOut]:
    """Create the signed-in Account's Player.

    The Account's own email address must be verified (403 otherwise), and
    an Account has one Player (409 for a second). The birth year and month
    come from the age held at signup in this session (ADR 0017); the body's
    are used only when the session holds none, and are then required. The
    held age is removed only once the Player exists, so a refused create
    can be retried. A player under 13 is refused with a fixed message.
    """
    account = request_account(request)
    if not _has_verified_email(account):
        raise HttpError(403, EMAIL_NOT_VERIFIED_MESSAGE)
    if _has_player(account):
        raise HttpError(409, ALREADY_HAS_PLAYER_MESSAGE)
    session = auth_session(request)
    held = peek_signup_birth(session, account)
    birth_year, birth_month = held or _birth_from_body(payload)
    try:
        player = create_player(
            account=account,
            display_name=payload.display_name,
            birth_year=birth_year,
            birth_month=birth_month,
            legal_name=payload.legal_name,
            gender_division=payload.gender_division,
            city=payload.city,
            region=payload.region,
        )
    except DjangoValidationError as error:
        # create_player locks the Account, so a concurrent second create
        # lands here.
        if _is_duplicate_player(error):
            raise HttpError(409, ALREADY_HAS_PLAYER_MESSAGE) from None
        raise _body_errors(error) from None
    taken = take_signup_birth(session, account)
    if taken is not None and session is not request.session:
        # The token's session: Django's middleware saves only the cookie's.
        # save() keeps the key, so the token still works.
        session.save()
    return Status(201, my_player_out(player, player.privacy))

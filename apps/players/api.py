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
the Display Name that ADR 0002 already shows to every signed-in Account,
and never a player whose Account is inactive. A query of at least three
characters, at most ``SEARCH_MAX_LIMIT`` results, and a per-Account
throttle (``SEARCH_MINUTE_RATE`` and ``SEARCH_DAY_RATE``) keep it from
listing every player. ninja checks throttles after auth and before it
parses the query, so a refused query counts toward the limit and an
unauthenticated request does not.
"""

import unicodedata
from typing import Annotated, Any

from django.db.models import Case, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Lower
from django.http import Http404, HttpRequest
from ninja import Field, Query, Router, Schema
from ninja.pagination import LimitOffsetPagination, paginate
from ninja.throttling import SimpleRateThrottle
from pydantic import BeforeValidator
from pydantic_core import PydanticCustomError

from apps.matches.api import request_account
from apps.matches.models import Match, MatchStatus
from apps.matches.schemas import MatchOut, match_out, with_match_details
from apps.players.models import Player
from apps.public_ids.api import PublicCodePath, get_by_public_code
from apps.public_ids.codes import CodeType
from config.api_errors import ErrorResponse
from config.auth import account_auth

router = Router(tags=["players"], auth=account_auth)

PlayerCode = Annotated[str, PublicCodePath(CodeType.PLAYER)]

HISTORY_PAGE_SIZE = 20
HISTORY_MAX_PAGE_SIZE = 100
# Postgres takes a bigint OFFSET: a larger one would fail in the database.
HISTORY_MAX_OFFSET = 2**63 - 1

SEARCH_MIN_LENGTH = 3
SEARCH_MAX_LENGTH = 64
SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 20
SEARCH_MINUTE_RATE = "30/m"
SEARCH_DAY_RATE = "300/d"
# Control characters and lone surrogates. Postgres refuses a NUL in a
# query parameter, which would be a 500.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})


class MatchHistoryPagination(LimitOffsetPagination):
    """Limit and offset paging of a match history, newest first.

    Each page's matches become ``MatchOut`` as the signed-in Account sees
    them (the Private player rule depends on the viewer).
    """

    class Input(Schema):
        limit: int = Field(HISTORY_PAGE_SIZE, ge=1, le=HISTORY_MAX_PAGE_SIZE)
        offset: int = Field(0, ge=0, le=HISTORY_MAX_OFFSET)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(max_limit=HISTORY_MAX_PAGE_SIZE, **kwargs)

    def paginate_queryset(
        self,
        queryset: QuerySet[Match],
        pagination: Any,
        request: HttpRequest,
        **params: Any,
    ) -> Any:
        page = super().paginate_queryset(queryset, pagination, request, **params)
        viewer = request_account(request)
        page[self.items_attribute] = [
            match_out(match, viewer) for match in page[self.items_attribute]
        ]
        return page


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


class AccountRateThrottle(SimpleRateThrottle):
    """A throttle counted per signed-in Account, keyed by its primary key.

    The key never uses ``str(request.auth)``, which is the Account's email.
    Give each instance its own ``scope``: two with one scope share a count.
    """

    def __init__(self, scope: str, rate: str) -> None:
        self.scope = scope
        super().__init__(rate=rate)

    def get_cache_key(self, request: HttpRequest) -> str | None:
        account = getattr(request, "auth", None)
        if account is None or getattr(account, "pk", None) is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": account.pk}


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
    throttle=[
        AccountRateThrottle("player_search_minute", SEARCH_MINUTE_RATE),
        AccountRateThrottle("player_search_day", SEARCH_DAY_RATE),
    ],
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

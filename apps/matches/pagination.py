"""Limit and offset paging of match lists, newest first.

Shared by a player's match history (``apps/players/api.py``), the
matches awaiting the signed-in player's confirmation, and the held
matches the signed-in Account may vouch for (both in
``apps/matches/api.py``). It lives here, not in either API module,
because ``apps.players.api`` imports ``apps.matches.api``.
"""

from typing import TYPE_CHECKING, Any, cast

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Field, Schema
from ninja.pagination import LimitOffsetPagination

from apps.matches.models import Match
from apps.matches.schemas import HoldOut, match_out
from apps.matches.workflow import vouched_at_own_level

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

HISTORY_PAGE_SIZE = 20
HISTORY_MAX_PAGE_SIZE = 100
# Postgres takes a bigint OFFSET: a larger one would fail in the database.
HISTORY_MAX_OFFSET = 2**63 - 1


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
        # The signed-in Account, as ``apps.matches.api.request_account``
        # reads it. Not imported from there, since that module imports
        # this one.
        viewer = cast("_User", request.auth)  # type: ignore[attr-defined]
        page[self.items_attribute] = [
            match_out(match, viewer) for match in page[self.items_attribute]
        ]
        return page


class HoldPagination(MatchHistoryPagination):
    """``MatchHistoryPagination`` whose items are ``HoldOut``.

    Each page's matches become a ``HoldOut``: the match as the signed-in
    Account sees it, and whether the Account's level has vouched for it.
    """

    def paginate_queryset(
        self,
        queryset: QuerySet[Match],
        pagination: Any,
        request: HttpRequest,
        **params: Any,
    ) -> Any:
        page = LimitOffsetPagination.paginate_queryset(
            self, queryset, pagination, request, **params
        )
        viewer = cast("_User", request.auth)  # type: ignore[attr-defined]
        page[self.items_attribute] = [
            HoldOut(
                match=match_out(match, viewer),
                vouched=vouched_at_own_level(match, viewer),
            )
            for match in page[self.items_attribute]
        ]
        return page

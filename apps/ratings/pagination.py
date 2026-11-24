"""Limit and offset paging of a player's rating history, newest first.

The same bounds as a match history (``apps.matches.pagination``): a limit
from 1 to 100, 20 by default, and an offset Postgres can take.
"""

from typing import Any

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Field, Schema
from ninja.pagination import LimitOffsetPagination

from apps.matches.pagination import (
    HISTORY_MAX_OFFSET,
    HISTORY_MAX_PAGE_SIZE,
    HISTORY_PAGE_SIZE,
)
from apps.ratings.models import RatingHistory
from apps.ratings.schemas import rating_history_item_out


class RatingHistoryPagination(LimitOffsetPagination):
    """Limit and offset paging; each row becomes a ``RatingHistoryItemOut``."""

    class Input(Schema):
        limit: int = Field(HISTORY_PAGE_SIZE, ge=1, le=HISTORY_MAX_PAGE_SIZE)
        offset: int = Field(0, ge=0, le=HISTORY_MAX_OFFSET)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(max_limit=HISTORY_MAX_PAGE_SIZE, **kwargs)

    def paginate_queryset(
        self,
        queryset: QuerySet[RatingHistory],
        pagination: Any,
        request: HttpRequest,
        **params: Any,
    ) -> Any:
        page = super().paginate_queryset(queryset, pagination, request, **params)
        page[self.items_attribute] = [
            rating_history_item_out(row) for row in page[self.items_attribute]
        ]
        return page

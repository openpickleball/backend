"""Response schemas for Ratings and rating history (issue #53).

A rating is always returned with its reliability: the OPEN standard
requires any app that shows an OPEN rating to show its reliability next
to it. Every rating is a demonstration rating while the engine is a
demonstration package, so every entry, and the public lookup's answer,
carries ``demonstration: true``. Values are returned as stored, never
rounded (hard rule 2). No row id, match code, or replay id is returned.
"""

from datetime import datetime
from typing import Final, Literal

from ninja import Field, Schema

from apps.matches.models import RatingFormat
from apps.ratings.models import RatingChangeKind, RatingHistory, RatingState

# True while the engine is a demonstration package (CLAUDE.md, decision Q4).
DEMONSTRATION_RATINGS: Final = True

_RELIABILITY = (
    "OPEN-earned reliability, 0 to 100. The OPEN standard requires any app "
    "that shows an OPEN rating to show its reliability next to it."
)
_IMPORTED_RELIABILITY = (
    "The reliability carried from an external rating, 0 to 100, shown apart "
    "from the OPEN-earned reliability. Null when there is none."
)
_PROVISIONAL = (
    "True when the rating holds live updates not yet confirmed by a replay of "
    "the ledger, or was never replayed. A provisional rating can change at "
    "the next replay."
)
_ESTABLISHED = "True once the rating rests on enough matches to be established."
_SPEC_VERSION = "The OPEN rating spec version the rating follows."
_DEMONSTRATION = (
    "Always true: every rating is a demonstration rating while the rating "
    "engine is a demonstration package."
)


class RatingOut(Schema):
    """A player's current rating in one rating format, with its reliability.

    The OPEN standard requires any app that shows an OPEN rating to show its
    reliability next to it. Every rating is a demonstration rating while
    the engine is a demonstration package.
    """

    rating: float = Field(description="The rating, as the engine gave it.")
    reliability: float = Field(description=_RELIABILITY)
    imported_reliability: float | None = Field(description=_IMPORTED_RELIABILITY)
    provisional: bool = Field(description=_PROVISIONAL)
    established: bool = Field(description=_ESTABLISHED)
    spec_version: str = Field(description=_SPEC_VERSION)
    demonstration: Literal[True] = Field(description=_DEMONSTRATION)
    updated_at: datetime = Field(description="When the rating last changed.")


class PlayerRatingsOut(Schema):
    """A player's current ratings, one per rating format the player has.

    The OPEN standard requires any app that shows an OPEN rating to show its
    reliability next to it. Every rating is a demonstration rating while
    the engine is a demonstration package.
    """

    code: str = Field(description="The player's OPEN Code.")
    profile_url: str = Field(description="The player's public profile page.")
    demonstration: Literal[True] = Field(description=_DEMONSTRATION)
    ratings: dict[RatingFormat, RatingOut] = Field(
        description=(
            "The player's ratings keyed by rating format. Only the formats "
            "the player has a rating in appear; empty when none."
        )
    )


class RatingHistoryItemOut(Schema):
    """One change to a player's rating in one rating format.

    ``kind`` says what caused it: a live update from one match, a replay of
    the ledger, or its removal by a replay that no longer produces it (the
    values are then the ones removed). The OPEN standard requires any app
    that shows an OPEN rating to show its reliability next to it. Every
    rating is a demonstration rating while the engine is a demonstration
    package.
    """

    rating_format: RatingFormat
    kind: RatingChangeKind
    rating: float = Field(description="The rating after the change.")
    reliability: float = Field(description=_RELIABILITY)
    imported_reliability: float | None = Field(description=_IMPORTED_RELIABILITY)
    provisional: bool = Field(description=_PROVISIONAL)
    established: bool = Field(description=_ESTABLISHED)
    spec_version: str = Field(description=_SPEC_VERSION)
    demonstration: Literal[True] = Field(description=_DEMONSTRATION)
    recorded_at: datetime = Field(description="When the change was recorded.")


def rating_out(state: RatingState) -> RatingOut:
    return RatingOut(
        rating=state.rating,
        reliability=state.reliability,
        imported_reliability=state.imported_reliability,
        provisional=state.provisional,
        established=state.established,
        spec_version=state.spec_version,
        demonstration=DEMONSTRATION_RATINGS,
        updated_at=state.updated_at,
    )


def rating_history_item_out(row: RatingHistory) -> RatingHistoryItemOut:
    return RatingHistoryItemOut(
        rating_format=RatingFormat(row.rating_format),
        kind=RatingChangeKind(row.kind),
        rating=row.rating,
        reliability=row.reliability,
        imported_reliability=row.imported_reliability,
        provisional=row.provisional,
        established=row.established,
        spec_version=row.spec_version,
        demonstration=DEMONSTRATION_RATINGS,
        recorded_at=row.created_at,
    )

"""Shared cache helpers for public player rating lookups (issue #54)."""

from django.core.cache import cache

from apps.ratings.schemas import PlayerRatingsOut

PUBLIC_RATINGS_CACHE_TTL = 300


def public_ratings_cache_key(code: str) -> str:
    """Return the cache key for a player's public ratings response."""
    return f"ratings:public:{code}"


def get_cached_player_ratings(code: str) -> PlayerRatingsOut | None:
    """Return the cached ``PlayerRatingsOut`` for ``code``, or ``None``."""
    cached = cache.get(public_ratings_cache_key(code))
    if cached is None:
        return None
    return PlayerRatingsOut.model_validate(cached)


def set_cached_player_ratings(code: str, payload: PlayerRatingsOut) -> None:
    """Store ``payload`` in the shared cache under ``code``."""
    cache.set(
        public_ratings_cache_key(code),
        payload.model_dump(mode="json"),
        timeout=PUBLIC_RATINGS_CACHE_TTL,
    )


def invalidate_player_ratings(code: str) -> None:
    """Delete the cached public ratings for ``code``."""
    cache.delete(public_ratings_cache_key(code))

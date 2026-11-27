"""Tests for caching public player rating lookups (issue #54)."""

from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import RatingFormat
from apps.players.models import Visibility
from apps.players.services import update_privacy
from apps.players.tests.factories import PlayerFactory
from apps.ratings.cache import public_ratings_cache_key
from apps.ratings.models import RatingChangeKind
from apps.ratings.services import apply_rating
from apps.ratings.tests.factories import make_state, rating_summary

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def test_second_lookup_is_served_from_cache(
    django_assert_num_queries: pytest.CaptureFixture[int],
) -> None:
    player = PlayerFactory.create()
    make_state(player=player, rating_format=RatingFormat.SINGLES, rating=3.5)
    viewer = Client()
    viewer.force_login(UserFactory.create())

    first = viewer.get(f"/api/v1/players/{player.public_code}/ratings")
    assert first.status_code == 200
    assert first.json()["ratings"]["singles"]["rating"] == 3.5
    assert cache.get(public_ratings_cache_key(player.public_code)) is not None

    # Second lookup should not query RatingState (only auth/player lookup).
    with django_assert_num_queries(2):
        second = viewer.get(f"/api/v1/players/{player.public_code}/ratings")
    assert second.status_code == 200
    assert second.json() == first.json()


def test_rating_change_invalidates_cached_lookup() -> None:
    player = PlayerFactory.create()
    make_state(player=player, rating_format=RatingFormat.SINGLES, rating=3.5)
    viewer = Client()
    viewer.force_login(UserFactory.create())

    first = viewer.get(f"/api/v1/players/{player.public_code}/ratings")
    assert first.status_code == 200
    assert first.json()["ratings"]["singles"]["rating"] == 3.5

    apply_rating(
        player=player,
        summary=rating_summary(rating_format=RatingFormat.SINGLES, rating=4.1),
        kind=RatingChangeKind.REPLAY,
    )
    assert cache.get(public_ratings_cache_key(player.public_code)) is None

    second = viewer.get(f"/api/v1/players/{player.public_code}/ratings")
    assert second.status_code == 200
    assert second.json()["ratings"]["singles"]["rating"] == 4.1


def test_privacy_opt_out_clears_cached_rating() -> None:
    player = PlayerFactory.create()
    make_state(player=player, rating_format=RatingFormat.SINGLES, rating=3.5)
    viewer = Client()
    viewer.force_login(UserFactory.create())

    first = viewer.get(f"/api/v1/players/{player.public_code}/ratings")
    assert first.status_code == 200
    assert cache.get(public_ratings_cache_key(player.public_code)) is not None

    update_privacy(
        player=player,
        actor=player.account,
        changes={"rating_visibility": Visibility.PRIVATE},
    )
    assert cache.get(public_ratings_cache_key(player.public_code)) is None

    after_opt_out = viewer.get(f"/api/v1/players/{player.public_code}/ratings")
    assert after_opt_out.status_code == 404

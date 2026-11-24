"""The public rating lookup shows only a player proven 18 or older (issue #53).

``not is_minor(...)`` is also true for an age under 13 and for a birth
month in the future, and raises for a missing birth year or month, so a
row written past the model's checks (``QuerySet.update``, ``bulk_create``,
``loaddata``) could reach the public. Only an active Account whose player
is 18 or older on the age limit date (UTC-12, ADR 0017) is public;
anything else gets the same 404 as an unknown code. A player counts as
born on the last day of their birth month, so they turn 18 at 12:00 UTC
on that day. Made-up data only.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.players.age import age_limit_date
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate
from apps.ratings.api import is_publicly_rated
from apps.ratings.tests.factories import make_state

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def lookup(code: str) -> Any:
    return Client().get(f"/api/v1/players/{code}/ratings")


def _rated_with_birth(year: int, month: int) -> Player:
    """A rated adult whose stored birth is then rewritten past the checks."""
    player: Player = PlayerFactory.create()
    make_state(player)
    Player.objects.filter(pk=player.pk).update(birth_year=year, birth_month=month)
    player.refresh_from_db()
    return player


def assert_hidden(player: Player) -> None:
    unknown = lookup(generate(CodeType.PLAYER))
    response = lookup(player.public_code)

    assert not is_publicly_rated(player)
    assert response.status_code == 404
    assert response.status_code == unknown.status_code
    assert response.json() == unknown.json()


def test_a_stored_age_of_12_is_hidden() -> None:
    today = age_limit_date()
    assert_hidden(_rated_with_birth(today.year - 12, 1))


def test_a_stored_birth_month_in_the_future_is_hidden() -> None:
    today = age_limit_date()
    assert_hidden(_rated_with_birth(today.year + 1, 1))


def test_a_missing_birth_year_or_month_is_not_public() -> None:
    account = UserFactory.create()
    for year, month in ((None, None), (None, 6), (1990, None)):
        # Built in memory: the database refuses an empty birth year or month.
        birth: dict[str, int | None] = {"birth_year": year, "birth_month": month}
        player = Player(account=account, **birth)
        assert not is_publicly_rated(player)


# The 18th birthday: the last day of the birth month, at UTC-12.


def _at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(timezone, "now", lambda: moment)


@pytest.fixture
def born_june_2008() -> Player:
    # 18 on 2026-06-30, the last day of the birth month.
    player: Player = PlayerFactory.create(birth_year=2008, birth_month=6)
    make_state(player)
    return player


def test_hidden_until_the_birthday_begins_at_utc_minus_12(
    monkeypatch: pytest.MonkeyPatch, born_june_2008: Player
) -> None:
    _at(monkeypatch, datetime(2026, 6, 30, 11, 59, tzinfo=UTC))

    assert age_limit_date().isoformat() == "2026-06-29"
    assert_hidden(born_june_2008)


def test_shown_from_noon_utc_on_the_last_day_of_the_birth_month(
    monkeypatch: pytest.MonkeyPatch, born_june_2008: Player
) -> None:
    _at(monkeypatch, datetime(2026, 6, 30, 12, 0, tzinfo=UTC))

    assert age_limit_date().isoformat() == "2026-06-30"
    assert is_publicly_rated(born_june_2008)
    response = lookup(born_june_2008.public_code)
    assert response.status_code == 200
    assert set(response.json()["ratings"]) == {"doubles"}

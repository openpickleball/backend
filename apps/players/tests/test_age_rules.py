"""The 13-and-older rule and the private default for 13 to 17 (issue #114)."""

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.players.age import age_limit_date, is_minor
from apps.players.models import Player, PlayerPrivacy, Visibility
from apps.players.services import create_player, update_profile
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.models import IssuedCode

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

REFUSED = "This birth year and month can't be used."
FUTURE = "The birth year and month cannot be in the future."


def _at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(timezone, "now", lambda: moment)


def _create(account: "_User", birth_year: int, birth_month: int) -> Player:
    return create_player(
        account=account,
        first_name="Made-up",
        last_name="Player",
        birth_year=birth_year,
        birth_month=birth_month,
        gender_division="mens",
    )


def _levels(player: Player) -> set[str]:
    privacy = PlayerPrivacy.objects.get(player=player)
    return {getattr(privacy, name) for name in PlayerPrivacy.SETTINGS}


def _nothing_written() -> bool:
    return not (
        Player.objects.exists()
        or PlayerPrivacy.objects.exists()
        or IssuedCode.objects.exists()
        or AuditEvent.objects.exists()
    )


# Under 13 is refused


def test_create_player_under_13_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Born 31 May 2013: 12 until 31 May 2026 at UTC-12.
    _at(monkeypatch, datetime(2026, 5, 31, 5, 0, tzinfo=UTC))
    account = UserFactory.create()

    with pytest.raises(ValidationError) as caught:
        _create(account, 2013, 5)

    assert caught.value.message_dict == {"birth_month": [REFUSED]}
    assert _nothing_written()


def test_create_player_on_the_13th_birthday_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _at(monkeypatch, datetime(2026, 5, 31, 12, 0, tzinfo=UTC))

    player = _create(UserFactory.create(), 2013, 5)

    assert Player.objects.filter(pk=player.pk).exists()


def test_this_month_is_refused_as_under_13_not_as_future() -> None:
    today = timezone.localdate()
    account = UserFactory.create()

    with pytest.raises(ValidationError) as caught:
        _create(account, today.year, today.month)

    assert caught.value.message_dict == {"birth_month": [REFUSED]}


def test_a_future_month_keeps_its_own_message() -> None:
    account = UserFactory.create()

    with pytest.raises(ValidationError) as caught:
        _create(account, timezone.localdate().year + 1, 1)

    assert caught.value.message_dict == {"birth_month": [FUTURE]}
    assert _nothing_written()


def test_stored_player_is_not_rechecked_when_another_field_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    year = age_limit_date().year - 5
    later = datetime(year + 20, 6, 1, 12, 0, tzinfo=UTC)
    with monkeypatch.context() as patch:
        _at(patch, later)
        player = _create(UserFactory.create(), year, 1)

    # Back at today, the stored player counts as under 13.
    update_profile(player=player, actor=player.account, changes={"city": "Madeupton"})

    player.refresh_from_db()
    assert player.city == "Madeupton"


# 13 to 17 start private


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Born 30 June 2008: 18 on 30 June 2026 at UTC-12.
        (datetime(2026, 6, 30, 5, 0, tzinfo=UTC), {Visibility.PRIVATE}),
        (datetime(2026, 6, 30, 12, 0, tzinfo=UTC), {Visibility.SIGNED_IN}),
    ],
)
def test_privacy_default_at_the_adult_limit(
    monkeypatch: pytest.MonkeyPatch, moment: datetime, expected: set[str]
) -> None:
    _at(monkeypatch, moment)

    player = _create(UserFactory.create(), 2008, 6)

    assert _levels(player) == expected


def test_a_13_year_old_starts_private(monkeypatch: pytest.MonkeyPatch) -> None:
    _at(monkeypatch, datetime(2026, 5, 31, 12, 0, tzinfo=UTC))

    player = _create(UserFactory.create(), 2013, 5)

    assert _levels(player) == {Visibility.PRIVATE}


def test_an_adult_gets_the_model_defaults() -> None:
    player = _create(UserFactory.create(), 1990, 6)

    assert _levels(player) == {Visibility.SIGNED_IN}


# Turning 18 changes nothing


def test_turning_18_keeps_every_setting_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _at(monkeypatch, datetime(2026, 6, 29, 12, 0, tzinfo=UTC))
    player = _create(UserFactory.create(), 2008, 6)
    assert is_minor(2008, 6)

    _at(monkeypatch, datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
    assert not is_minor(2008, 6)
    update_profile(player=player, actor=player.account, changes={"city": "Madeupton"})

    assert _levels(player) == {Visibility.PRIVATE}


# The factory


def test_factory_default_is_an_adult_on_any_day_of_the_year() -> None:
    year = age_limit_date().year
    for player in PlayerFactory.create_batch(20):
        assert player.birth_year <= year - 19
        assert not is_minor(player.birth_year, player.birth_month, date(year, 1, 1))
        assert not is_minor(player.birth_year, player.birth_month, date(year, 12, 31))
        assert _levels(player) == {Visibility.SIGNED_IN}


def test_factory_minor_trait_is_13_to_17_on_any_day_of_the_year() -> None:
    year = age_limit_date().year
    for player in PlayerFactory.create_batch(10, minor=True):
        assert is_minor(player.birth_year, player.birth_month, date(year, 1, 1))
        assert is_minor(player.birth_year, player.birth_month, date(year, 12, 31))
        assert _levels(player) == {Visibility.PRIVATE}

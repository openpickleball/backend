"""The Player model: its Account link, fields, rules, and OPEN Code."""

import re
from typing import TYPE_CHECKING, Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.players.models import GenderDivision, Player, PlayerPrivacy

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

CODE_PATTERN = re.compile(r"^P-[0-9A-Z]{4}-[0-9A-Z]{4}$")

# A made-up value that must never show up in a validation message.
MARKER = "zqxjmarker"


def _valid(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "display_name": "Made-up Player",
        "birth_year": 1990,
        "birth_month": 6,
        "gender_division": "mens",
    }
    values.update(overrides)
    return values


def _player(account: "_User | None" = None, **overrides: Any) -> Player:
    return Player.objects.create(
        account=account or UserFactory.create(), **_valid(**overrides)
    )


def _messages(error: ValidationError) -> str:
    return " ".join(
        message for messages in error.message_dict.values() for message in messages
    )


def test_player_fields_are_exactly_the_planned_set() -> None:
    names = {field.name for field in Player._meta.concrete_fields}

    assert names == {
        "id",
        "public_code",
        "account",
        "display_name",
        "legal_name",
        "birth_year",
        "birth_month",
        "gender_division",
        "city",
        "region",
    }


def test_privacy_fields_are_exactly_the_five_settings() -> None:
    names = {field.name for field in PlayerPrivacy._meta.concrete_fields}

    assert names == {"player", *PlayerPrivacy.SETTINGS}
    assert PlayerPrivacy.SETTINGS == (
        "city_region_visibility",
        "match_history_visibility",
        "rating_history_visibility",
        "stats_visibility",
        "home_club_visibility",
        "rating_visibility",
    )


def test_player_belongs_to_its_account() -> None:
    account = UserFactory.create()

    player = _player(account)

    assert player.account == account
    assert account.player == player


def test_second_player_for_one_account_fails() -> None:
    account = UserFactory.create()
    _player(account)

    with pytest.raises(IntegrityError), transaction.atomic():
        _player(account, display_name="Another Made-up Player")


def test_player_needs_an_account() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Player.objects.create(account=None, **_valid())  # type: ignore[misc]


@pytest.mark.parametrize("field", ["birth_year", "birth_month"])
def test_player_without_birth_year_or_month_fails_in_the_database(
    field: str,
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        _player(**{field: None})


def test_deleting_an_account_with_a_player_is_refused() -> None:
    account = UserFactory.create()
    _player(account)

    with pytest.raises(ProtectedError):
        account.delete()


def test_str_is_the_open_code_not_a_name() -> None:
    player = _player(display_name="Made-up Display", legal_name="Made-up Legal")

    assert str(player) == player.public_code
    assert CODE_PATTERN.match(str(player))
    assert "Made-up" not in str(player)


def test_privacy_str_is_the_open_code() -> None:
    player = _player()
    privacy = PlayerPrivacy.objects.create(player=player)

    assert str(privacy) == player.public_code


def test_valid_player_passes_full_clean() -> None:
    player = Player(
        account=UserFactory.create(),
        legal_name="Made-up Legal Name",
        city="Madeupville",
        region="Made-up Region",
        **_valid(gender_division=GenderDivision.WOMENS),
    )

    player.full_clean()


@pytest.mark.parametrize("value", ["mens", "womens"])
def test_gender_division_accepts_each_value(value: str) -> None:
    player = Player(account=UserFactory.create(), **_valid(gender_division=value))

    player.full_clean()


@pytest.mark.parametrize("value", ["", None])
def test_a_missing_gender_division_is_refused(value: object) -> None:
    # Every Player has a gender division (issue #215).
    player = Player(account=UserFactory.create(), **_valid(gender_division=value))

    with pytest.raises(ValidationError) as caught:
        player.full_clean()

    assert caught.value.message_dict == {
        "gender_division": ["Choose a gender division."]
    }


def test_the_database_still_allows_an_empty_gender_division() -> None:
    # Kept so a later removal (#190, #116) can clear it.
    player = _player()

    Player.objects.filter(pk=player.pk).update(gender_division="")

    player.refresh_from_db()
    assert player.gender_division == ""


def test_gender_division_values() -> None:
    assert set(GenderDivision.values) == {"mens", "womens"}


def test_full_clean_strips_text_fields() -> None:
    player = Player(
        account=UserFactory.create(),
        legal_name="  Made-up Legal  ",
        city=" Madeupville ",
        region=" Made-up Region ",
        **_valid(display_name="  Made-up Player  "),
    )

    player.full_clean()

    assert player.display_name == "Made-up Player"
    assert player.legal_name == "Made-up Legal"
    assert player.city == "Madeupville"
    assert player.region == "Made-up Region"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_display_name_is_refused(value: str) -> None:
    player = Player(account=UserFactory.create(), **_valid(display_name=value))

    with pytest.raises(ValidationError) as caught:
        player.full_clean()

    assert "display_name" in caught.value.message_dict


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gender_division", MARKER),
        ("birth_year", MARKER),
        ("birth_month", MARKER),
        ("birth_year", 1899),
        ("birth_month", 0),
        ("birth_month", 13),
        ("display_name", MARKER * 10),
        ("legal_name", MARKER * 30),
        ("city", MARKER * 15),
        ("region", MARKER * 15),
    ],
)
def test_invalid_value_is_refused_without_echoing_it(field: str, value: object) -> None:
    player = Player(account=UserFactory.create(), **_valid(**{field: value}))

    with pytest.raises(ValidationError) as caught:
        player.full_clean()

    assert field in caught.value.message_dict
    text = _messages(caught.value)
    assert MARKER not in text


def test_birth_month_in_the_future_is_refused() -> None:
    today = timezone.localdate()
    year, month = (
        (today.year, today.month + 1)
        if today.month < 12
        else (
            today.year + 1,
            1,
        )
    )
    player = Player(
        account=UserFactory.create(), **_valid(birth_year=year, birth_month=month)
    )

    with pytest.raises(ValidationError) as caught:
        player.full_clean()

    text = _messages(caught.value)
    assert str(year) not in text


def test_birth_month_this_month_is_not_in_the_future() -> None:
    # Not future, though under 13, so the age rule refuses it (issue #114).
    today = timezone.localdate()
    player = Player(
        account=UserFactory.create(),
        **_valid(birth_year=today.year, birth_month=today.month),
    )

    with pytest.raises(ValidationError) as caught:
        player.full_clean()

    assert caught.value.message_dict == {
        "birth_month": ["This birth year and month can't be used."]
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("display_name", ""),
        ("birth_month", 0),
        ("birth_month", 13),
        ("birth_year", 1899),
        ("gender_division", "other"),
    ],
)
def test_database_refuses_raw_invalid_writes(field: str, value: object) -> None:
    player = _player()

    with pytest.raises(IntegrityError), transaction.atomic():
        Player.objects.filter(pk=player.pk).update(**{field: value})

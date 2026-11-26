"""Player.save() checks the encrypted fields (issue #115, ADR 0046).

The database cannot check an encrypted value, so the check constraints on
birth year, birth month, and gender division were dropped and save()
applies the same rules, with the model's fixed messages.
"""

from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.players.models import Player

pytestmark = pytest.mark.django_db

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


def _messages(error: ValidationError) -> str:
    return " ".join(m for ms in error.message_dict.values() for m in ms)


BAD_VALUES = [
    ("birth_year", 1899),
    ("birth_year", 0),
    ("birth_year", "1990"),
    ("birth_year", 1990.0),
    ("birth_year", True),
    ("birth_year", MARKER),
    ("birth_month", 0),
    ("birth_month", 13),
    ("birth_month", "6"),
    ("birth_month", 6.0),
    ("birth_month", MARKER),
    ("gender_division", "other"),
    ("gender_division", MARKER),
    ("gender_division", 1),
]


@pytest.mark.parametrize(("field", "value"), BAD_VALUES)
def test_create_refuses_a_bad_value_without_echoing_it(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError) as caught:
        Player.objects.create(account=UserFactory.create(), **_valid(**{field: value}))

    assert set(caught.value.message_dict) == {field}
    assert MARKER not in _messages(caught.value)
    assert not Player.objects.exists()


@pytest.mark.parametrize(("field", "value"), BAD_VALUES)
def test_update_fields_save_naming_the_field_refuses(field: str, value: object) -> None:
    player = Player.objects.create(account=UserFactory.create(), **_valid())
    setattr(player, field, value)

    with pytest.raises(ValidationError) as caught:
        player.save(update_fields=[field])

    assert set(caught.value.message_dict) == {field}
    player.refresh_from_db()
    assert getattr(player, field) == _valid()[field]


def test_update_fields_save_not_naming_a_bad_field_saves() -> None:
    player = Player.objects.create(account=UserFactory.create(), **_valid())
    player.birth_month = 13
    player.city = "Madeupville"

    player.save(update_fields=["city"])

    player.refresh_from_db()
    assert player.city == "Madeupville"
    assert player.birth_month == 6


@pytest.mark.parametrize("division", ["", "mens", "womens"])
def test_valid_values_save(division: str) -> None:
    player = Player.objects.create(
        account=UserFactory.create(),
        **_valid(birth_year=1900, birth_month=12, gender_division=division),
    )

    player.refresh_from_db()
    assert (player.birth_year, player.birth_month) == (1900, 12)
    assert player.gender_division == division


def test_fixed_messages() -> None:
    with pytest.raises(ValidationError) as caught:
        Player.objects.create(
            account=UserFactory.create(),
            **_valid(birth_year=MARKER, birth_month=0, gender_division=MARKER),
        )

    assert caught.value.message_dict == {
        "birth_year": ["Enter a whole number."],
        "birth_month": ["Ensure this value is greater than or equal to 1."],
        "gender_division": ["Choose one of the listed options."],
    }

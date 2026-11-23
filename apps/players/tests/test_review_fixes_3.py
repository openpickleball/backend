"""Review fixes for display name visibility (C12, C13)."""

from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.players.models import Player
from apps.players.services import create_player

pytestmark = pytest.mark.django_db


def _create(**overrides: Any) -> Player:
    values: dict[str, Any] = {
        "account": UserFactory.create(),
        "display_name": "Made-up Player",
        "birth_year": 1990,
        "birth_month": 6,
        "gender_division": "mens",
    }
    values.update(overrides)
    return create_player(**values)


# C12: the null notehead renders blank.


@pytest.mark.parametrize("value", ["\U0001d159", "\U0001d159\U0001d159"])
def test_c12_null_notehead_display_name_is_refused(value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _create(display_name=value)

    assert "display_name" in caught.value.message_dict
    assert not Player.objects.exists()


# C13: characters newer than Python's Unicode data (Cn) count as visible.


@pytest.mark.parametrize(
    "value",
    [
        "\U0001fae9",  # a Unicode 16 emoji
        "\U00010d50\U00010d51",  # Garay letters, Unicode 16
    ],
)
def test_c13_newer_unicode_display_name_is_accepted(value: str) -> None:
    created = _create(display_name=value)

    assert Player.objects.get(pk=created.pk).display_name == value


@pytest.mark.parametrize("value", ["́", "", "\U000f0000"])
def test_c13_combining_or_private_use_alone_is_still_refused(value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _create(display_name=value)

    assert "display_name" in caught.value.message_dict

"""Player.save() reads a one-shot ``update_fields`` iterable once
(issue #115, ADR 0046).

The check and the save must see the same fields: a generator consumed
by the check would save nothing, and an iterator consumed by the save
would skip the check.
"""

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.players.models import Player

pytestmark = pytest.mark.django_db


def _player() -> Player:
    return Player.objects.create(
        account=UserFactory.create(),
        display_name="Made-up Player",
        birth_year=1990,
        birth_month=6,
        gender_division="mens",
    )


def test_a_generator_of_fields_still_saves_them() -> None:
    player = _player()
    player.display_name = "Renamed Made-up"

    player.save(update_fields=(name for name in ["display_name"]))

    player.refresh_from_db()
    assert player.display_name == "Renamed Made-up"


def test_an_iterator_of_fields_is_still_checked() -> None:
    player = _player()
    player.gender_division = "other"

    with pytest.raises(ValidationError) as caught:
        player.save(update_fields=iter(["gender_division"]))

    assert set(caught.value.message_dict) == {"gender_division"}
    player.refresh_from_db()
    assert player.gender_division == "mens"

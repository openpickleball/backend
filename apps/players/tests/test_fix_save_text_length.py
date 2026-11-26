"""Player.save() checks the encrypted text fields' type and length
(issue #115, ADR 0046).

Their columns are text now, so the database no longer limits a legal
name to 200 characters or a gender division to 8. save() refuses a
value that is not text or is too long, with a fixed message that never
holds the value.
"""

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.players.models import Player

pytestmark = pytest.mark.django_db

MARKER = "zqxjmarker"


def _new(**values: object) -> Player:
    fields: dict[str, object] = {
        "display_name": "Made-up Player",
        "birth_year": 1990,
        "birth_month": 6,
        "gender_division": "mens",
        **values,
    }
    return Player(account=UserFactory.create(), **fields)


def _text(error: ValidationError) -> str:
    return " ".join(m for ms in error.message_dict.values() for m in ms)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("legal_name", MARKER * 21),
        ("legal_name", "x" * 500),
        ("legal_name", 5),
        ("legal_name", b"bytes"),
        ("gender_division", 7),
    ],
)
def test_save_refuses_a_long_or_non_text_value(field: str, value: object) -> None:
    player = _new(**{field: value})

    with pytest.raises(ValidationError) as caught:
        player.save()

    assert set(caught.value.message_dict) == {field}
    assert MARKER not in _text(caught.value)
    assert not Player.objects.exists()


def test_an_update_fields_save_naming_legal_name_refuses_a_long_one() -> None:
    player = _new()
    player.save()
    player.legal_name = "x" * 201

    with pytest.raises(ValidationError) as caught:
        player.save(update_fields=["legal_name"])

    assert set(caught.value.message_dict) == {"legal_name"}
    player.refresh_from_db()
    assert player.legal_name == ""


def test_a_legal_name_at_the_limit_saves() -> None:
    player = _new(legal_name="x" * 200)

    player.save()

    player.refresh_from_db()
    assert player.legal_name == "x" * 200

"""Player personal data fields are stored encrypted (issue #115, ADR 0046)."""

import pytest
from django.core.exceptions import FieldError
from django.db import connection

from apps.accounts.tests.factories import UserFactory
from apps.encryption.keys import is_token
from apps.players.models import Player

pytestmark = pytest.mark.django_db

ENCRYPTED = ("legal_name", "birth_year", "birth_month", "gender_division")
LEGAL_NAME = "Zyqworth Made-up Legalname"


def _player() -> Player:
    return Player.objects.create(
        account=UserFactory.create(),
        display_name="Made-up Display",
        legal_name=LEGAL_NAME,
        birth_year=1987,
        birth_month=11,
        gender_division="womens",
        city="Madeupville",
    )


def _raw_row(player: Player) -> tuple[str, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT legal_name, birth_year, birth_month, gender_division "
            "FROM players_player WHERE id = %s",
            [player.pk],
        )
        row = cursor.fetchone()
    assert row is not None
    return tuple(row)


def test_raw_columns_hold_tokens_not_plain_text() -> None:
    player = _player()

    row = _raw_row(player)

    for value in row:
        assert isinstance(value, str)
        assert is_token(value)
    joined = " ".join(row)
    for plain in (LEGAL_NAME, "Zyqworth", "1987", "womens"):
        assert plain not in joined
    # The month is a short number: no column holds it as the whole value.
    assert "11" not in row


def test_the_orm_reads_back_the_python_values() -> None:
    player = _player()

    stored = Player.objects.get(pk=player.pk)

    assert stored.legal_name == LEGAL_NAME
    assert stored.birth_year == 1987
    assert isinstance(stored.birth_year, int)
    assert stored.birth_month == 11
    assert isinstance(stored.birth_month, int)
    assert stored.gender_division == "womens"


def test_values_list_and_values_decrypt() -> None:
    player = _player()

    assert list(
        Player.objects.filter(pk=player.pk).values_list("gender_division", "birth_year")
    ) == [("womens", 1987)]
    assert list(Player.objects.filter(pk=player.pk).values("legal_name")) == [
        {"legal_name": LEGAL_NAME}
    ]


@pytest.mark.parametrize("name", ENCRYPTED)
@pytest.mark.parametrize("lookup", ["", "__exact", "__in", "__icontains", "__gte"])
def test_lookups_other_than_isnull_are_refused(name: str, lookup: str) -> None:
    value: object = [1] if lookup == "__in" else 1
    with pytest.raises(FieldError):
        Player.objects.filter(**{f"{name}{lookup}": value}).exists()


@pytest.mark.parametrize("name", ENCRYPTED)
def test_isnull_lookup_works(name: str) -> None:
    player = _player()

    assert Player.objects.filter(pk=player.pk, **{f"{name}__isnull": False}).exists()


def test_display_name_and_place_stay_plain() -> None:
    player = _player()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT display_name, city FROM players_player WHERE id = %s", [player.pk]
        )
        assert cursor.fetchone() == ("Made-up Display", "Madeupville")

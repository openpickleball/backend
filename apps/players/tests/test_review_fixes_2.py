"""Review fixes for Player text fields (C10, C11)."""

from typing import TYPE_CHECKING, Any

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.players.models import Player
from apps.players.services import create_player, update_profile

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

TEXT_FIELDS = ["display_name", "legal_name", "city", "region"]


@pytest.fixture
def owner() -> "_User":
    return UserFactory.create()


@pytest.fixture
def player(owner: "_User") -> Player:
    return create_player(
        account=owner,
        display_name="Made-up Player",
        birth_year=1990,
        birth_month=6,
        gender_division="mens",
        city="Madeupville",
    )


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


# C10: a lone surrogate is refused, not left to fail at save.


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("value", ["Zq\ud800", "\udfff", "Zq\udc00xj"])
def test_c10_surrogate_is_refused_on_create(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _create(**{field: value})

    assert field in caught.value.message_dict
    assert "Zq" not in str(caught.value)
    assert not Player.objects.exists()


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_c10_surrogate_is_refused_on_update(
    player: Player, owner: "_User", field: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        update_profile(player=player, actor=owner, changes={field: "Zq\ud800"})

    assert field in caught.value.message_dict
    assert "Zq" not in str(caught.value)
    stored = Player.objects.get(pk=player.pk)
    assert stored.display_name == "Made-up Player"
    assert not AuditEvent.objects.exists()


# C11: a display name that renders blank is refused.


@pytest.mark.parametrize(
    "value",
    [
        "ㅤ",  # Hangul Filler
        "ᅟ",  # Hangul Choseong Filler
        "ᅠ",  # Hangul Jungseong Filler
        "ﾠ",  # Halfwidth Hangul Filler
        "⠀",  # Braille Pattern Blank
        "́",  # lone combining acute accent
        "\U000f0000",  # private use
        "",  # private use
        "ㅤ́​ ⠀",
    ],
)
def test_c11_blank_rendering_display_name_is_refused(value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _create(display_name=value)

    assert "display_name" in caught.value.message_dict
    assert not Player.objects.exists()


@pytest.mark.parametrize(
    "value",
    [
        "Made-up Player",
        "José",  # combining mark next to a letter
        "李",  # a CJK letter
        "7",
        "!",
        "★",  # a symbol
        "Ab",  # private use next to a letter
    ],
)
def test_c11_visible_display_name_is_accepted(value: str) -> None:
    created = _create(display_name=value)

    assert Player.objects.get(pk=created.pk).display_name == value

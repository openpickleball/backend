"""Player links and fields refuse a bad value without echoing it (issue #162).

Django's foreign key messages name the submitted id. The Player's
Account link and the privacy row's Player link give fixed text instead.
The encrypted fields already name no value; these tests pin that. Made-up
data only.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import models

from apps.players.models import Player, PlayerPrivacy
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

MARKER = "zqxjmarker"
UNKNOWN_ID = 987654321
BAD_IDS = [UNKNOWN_ID, MARKER]


def _messages(instance: models.Model) -> dict[str, list[str]]:
    with pytest.raises(ValidationError) as excinfo:
        instance.full_clean()
    return excinfo.value.message_dict


def _text(messages: dict[str, list[str]]) -> str:
    return " ".join(" ".join(items) for items in messages.values())


@pytest.mark.parametrize("bad", BAD_IDS)
def test_player_account_bad_id(bad: object) -> None:
    player = Player.objects.get(pk=PlayerFactory.create().pk)
    player.account_id = bad  # type: ignore[assignment]

    messages = _messages(player)

    assert messages["account"] == ["Choose an existing account."]
    assert MARKER not in _text(messages)
    assert str(UNKNOWN_ID) not in _text(messages)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_privacy_player_bad_id(bad: object) -> None:
    player = PlayerFactory.create()
    privacy = PlayerPrivacy.objects.get(pk=player.pk)
    privacy.player_id = bad  # type: ignore[assignment]

    messages = _messages(privacy)

    assert messages["player"] == ["Choose an existing player."]
    assert MARKER not in _text(messages)
    assert str(UNKNOWN_ID) not in _text(messages)


def test_encrypted_fields_name_no_value() -> None:
    player = Player.objects.get(pk=PlayerFactory.create().pk)
    player.legal_name = MARKER * 30
    player.birth_year = MARKER  # type: ignore[assignment]
    player.birth_month = MARKER  # type: ignore[assignment]
    player.gender_division = MARKER

    messages = _messages(player)

    assert {"legal_name", "birth_year", "birth_month", "gender_division"} <= set(
        messages
    )
    assert MARKER not in _text(messages)

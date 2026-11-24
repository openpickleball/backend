"""Who may see a player's current Ratings: ``rating_visibility`` (issue #53).

A player aged 13 to 17 starts with every privacy setting private, current
Ratings included, and nothing changes at 18. Only the player can widen it
to signed-in Accounts, and only once 18 or older (the age limit date, ADR
0017). Nobody can hide current Ratings: an adult's are always shown to
signed-in Accounts (ADR 0002). Sending the stored value is always
accepted. A refusal is a fixed message on ``rating_visibility`` that never
repeats the value, raised before anything is written. The model default is
private, so a row made outside ``create_player()`` fails closed. Made-up
data only.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.players.models import (
    PRIVACY_SETTINGS,
    GenderDivision,
    Player,
    PlayerPrivacy,
    Visibility,
)
from apps.players.services import update_privacy
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

ME_PLAYER = "/api/v1/me/player"
SETTING = "rating_visibility"
FIELD = "body.privacy.rating_visibility"
# Born June 2008: 18 from 2026-06-30 at UTC-12, that is 12:00 UTC.
BEFORE_18 = datetime(2026, 6, 30, 11, 59, tzinfo=UTC)
AT_18 = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


def _at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(timezone, "now", lambda: moment)


def _stored(player: Player) -> str:
    return str(PlayerPrivacy.objects.get(player=player).rating_visibility)


def _privacy_events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.PRIVACY_CHANGE))


def _message(error: ValidationError) -> list[str]:
    return list(error.message_dict[SETTING])


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def patch(client: Client, data: Any) -> Any:
    return client.patch(ME_PLAYER, data, content_type="application/json")


def fields(response: Any) -> dict[str, tuple[str, str]]:
    return {
        error["field"]: (error["code"], error["message"])
        for error in response.json()["errors"]
    }


@pytest.fixture
def adult() -> Player:
    return PlayerFactory.create()


@pytest.fixture
def minor_turning_18(monkeypatch: pytest.MonkeyPatch) -> Player:
    """A player created at 17, the minute before their 18th birthday."""
    _at(monkeypatch, BEFORE_18)
    player: Player = PlayerFactory.create(birth_year=2008, birth_month=6)
    return player


# The setting


def test_the_setting_is_the_last_privacy_setting() -> None:
    assert PRIVACY_SETTINGS[-1] == SETTING
    assert PlayerPrivacy.SETTINGS[-1] == SETTING
    assert PRIVACY_SETTINGS[:-1] == (
        "city_region_visibility",
        "match_history_visibility",
        "rating_history_visibility",
        "stats_visibility",
        "home_club_visibility",
    )


def test_the_model_default_is_private() -> None:
    player = Player.objects.create(
        account=UserFactory.create(),
        display_name="Raw M.",
        birth_year=1990,
        birth_month=1,
        gender_division=GenderDivision.MENS,
    )

    privacy = PlayerPrivacy.objects.create(player=player)

    privacy.refresh_from_db()
    assert privacy.rating_visibility == Visibility.PRIVATE


def test_an_adult_starts_signed_in(adult: Player) -> None:
    assert _stored(adult) == Visibility.SIGNED_IN


def test_a_minor_starts_private() -> None:
    minor = PlayerFactory.create(minor=True)

    assert _stored(minor) == Visibility.PRIVATE


def test_an_unknown_level_is_refused(adult: Player) -> None:
    with pytest.raises(ValidationError) as raised:
        update_privacy(player=adult, actor=adult.account, changes={SETTING: "zqxj"})

    assert SETTING in raised.value.message_dict
    assert _stored(adult) == Visibility.SIGNED_IN


# The service


def test_an_adult_cannot_hide_current_ratings(adult: Player) -> None:
    with pytest.raises(ValidationError) as raised:
        update_privacy(
            player=adult,
            actor=adult.account,
            changes={SETTING: Visibility.PRIVATE},
        )

    assert _message(raised.value)
    assert all("private" not in text for text in _message(raised.value))
    assert _stored(adult) == Visibility.SIGNED_IN
    assert _privacy_events() == []


def test_a_minor_cannot_widen_current_ratings() -> None:
    minor = PlayerFactory.create(minor=True)

    with pytest.raises(ValidationError) as raised:
        update_privacy(
            player=minor,
            actor=minor.account,
            changes={SETTING: Visibility.SIGNED_IN},
        )

    assert _message(raised.value)
    assert all("signed_in" not in text for text in _message(raised.value))
    assert _stored(minor) == Visibility.PRIVATE
    assert _privacy_events() == []


def test_hiding_and_widening_have_different_messages(adult: Player) -> None:
    minor = PlayerFactory.create(minor=True)
    with pytest.raises(ValidationError) as hide:
        update_privacy(
            player=adult, actor=adult.account, changes={SETTING: Visibility.PRIVATE}
        )
    with pytest.raises(ValidationError) as widen:
        update_privacy(
            player=minor, actor=minor.account, changes={SETTING: Visibility.SIGNED_IN}
        )

    assert _message(hide.value) != _message(widen.value)


def test_a_refused_widen_changes_no_other_setting() -> None:
    minor = PlayerFactory.create(minor=True)

    with pytest.raises(ValidationError):
        update_privacy(
            player=minor,
            actor=minor.account,
            changes={
                "stats_visibility": Visibility.SIGNED_IN,
                SETTING: Visibility.SIGNED_IN,
            },
        )

    privacy = PlayerPrivacy.objects.get(player=minor)
    assert privacy.stats_visibility == Visibility.PRIVATE
    assert _privacy_events() == []


def test_sending_the_stored_value_is_accepted(adult: Player) -> None:
    minor = PlayerFactory.create(minor=True)

    update_privacy(
        player=adult, actor=adult.account, changes={SETTING: Visibility.SIGNED_IN}
    )
    update_privacy(
        player=minor, actor=minor.account, changes={SETTING: Visibility.PRIVATE}
    )

    assert _stored(adult) == Visibility.SIGNED_IN
    assert _stored(minor) == Visibility.PRIVATE
    assert _privacy_events() == []


def test_a_player_who_turned_18_can_widen(
    monkeypatch: pytest.MonkeyPatch, minor_turning_18: Player
) -> None:
    player = minor_turning_18
    assert _stored(player) == Visibility.PRIVATE
    with pytest.raises(ValidationError):
        update_privacy(
            player=player, actor=player.account, changes={SETTING: Visibility.SIGNED_IN}
        )

    _at(monkeypatch, AT_18)
    update_privacy(
        player=player, actor=player.account, changes={SETTING: Visibility.SIGNED_IN}
    )

    assert _stored(player) == Visibility.SIGNED_IN
    [event] = _privacy_events()
    assert event.before == {SETTING: "private"}
    assert event.after == {SETTING: "signed_in"}


def test_a_player_who_widened_cannot_hide_again(
    monkeypatch: pytest.MonkeyPatch, minor_turning_18: Player
) -> None:
    player = minor_turning_18
    _at(monkeypatch, AT_18)
    update_privacy(
        player=player, actor=player.account, changes={SETTING: Visibility.SIGNED_IN}
    )

    with pytest.raises(ValidationError):
        update_privacy(
            player=player, actor=player.account, changes={SETTING: Visibility.PRIVATE}
        )

    assert _stored(player) == Visibility.SIGNED_IN


# PATCH /api/v1/me/player


def test_get_shows_the_setting(adult: Player) -> None:
    body = signed_in(adult.account).get(ME_PLAYER).json()

    assert body["privacy"][SETTING] == "signed_in"


def test_a_minor_widening_through_patch_gets_422() -> None:
    minor = PlayerFactory.create(minor=True)

    response = patch(signed_in(minor.account), {"privacy": {SETTING: "signed_in"}})

    assert response.status_code == 422
    assert FIELD in fields(response)
    assert "signed_in" not in fields(response)[FIELD][1]
    assert _stored(minor) == Visibility.PRIVATE
    assert _privacy_events() == []


def test_an_adult_hiding_through_patch_gets_422() -> None:
    adult = PlayerFactory.create(region="Ohio")

    response = patch(
        signed_in(adult.account),
        {"region": "Iowa", "privacy": {SETTING: "private"}},
    )

    assert response.status_code == 422
    assert FIELD in fields(response)
    assert _stored(adult) == Visibility.SIGNED_IN
    adult.refresh_from_db()
    assert adult.region == "Ohio"
    assert AuditEvent.objects.count() == 0


def test_patch_with_the_stored_value_is_accepted(adult: Player) -> None:
    response = patch(signed_in(adult.account), {"privacy": {SETTING: "signed_in"}})

    assert response.status_code == 200
    assert response.json()["privacy"][SETTING] == "signed_in"
    assert _privacy_events() == []


def test_patch_widens_once_18(
    monkeypatch: pytest.MonkeyPatch, minor_turning_18: Player
) -> None:
    client = signed_in(minor_turning_18.account)
    assert patch(client, {"privacy": {SETTING: "signed_in"}}).status_code == 422

    _at(monkeypatch, AT_18)
    response = patch(client, {"privacy": {SETTING: "signed_in"}})

    assert response.status_code == 200
    assert response.json()["privacy"][SETTING] == "signed_in"
    assert len(_privacy_events()) == 1

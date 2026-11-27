"""The name format privacy setting (issue #354, ADR 0062).

The display name is built from the first and last name given at signup:
"First Last" (``full``) or "First L." (``first_last_initial``). An adult
starts with ``full`` and may switch; a player aged 13 to 17 has
``first_last_initial`` and cannot switch to ``full`` before 18. Nothing
changes at 18. Made-up names only.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.players.models import NameFormat, Player, PlayerPrivacy
from apps.players.services import (
    FULL_NAME_FROM_18_MESSAGE,
    create_player,
    is_name_format_locked,
    update_privacy,
)
from apps.players.tests.factories import PlayerFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


def _at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(timezone, "now", lambda: moment)


def _create(account: "_User", birth_year: int, birth_month: int) -> Player:
    return create_player(
        account=account,
        first_name="Quenby",
        last_name="Marlowfield",
        birth_year=birth_year,
        birth_month=birth_month,
        gender_division="womens",
    )


def _set(player: Player, value: str) -> PlayerPrivacy:
    return update_privacy(
        player=player, actor=player.account, changes={"name_format": value}
    )


def _stored_name(player: Player) -> str:
    return Player.objects.get(pk=player.pk).display_name


def test_an_adult_starts_with_the_full_name() -> None:
    player = _create(UserFactory.create(), 1990, 6)

    assert player.display_name == "Quenby Marlowfield"
    assert player.legal_name == "Quenby Marlowfield"
    assert player.privacy.name_format == NameFormat.FULL
    assert not is_name_format_locked(player)


def test_a_minor_starts_with_the_last_initial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _at(monkeypatch, datetime(2026, 6, 15, 12, tzinfo=UTC))
    player = _create(UserFactory.create(), 2011, 1)

    assert player.display_name == "Quenby M."
    assert player.legal_name == "Quenby Marlowfield"
    assert player.privacy.name_format == NameFormat.FIRST_LAST_INITIAL
    assert is_name_format_locked(player)


def test_an_adult_switches_to_the_initial_and_back() -> None:
    player = PlayerFactory.create(first_name="Quenby", last_name="Marlowfield")

    _set(player, NameFormat.FIRST_LAST_INITIAL)

    assert _stored_name(player) == "Quenby M."
    assert player.display_name == "Quenby M."

    _set(player, NameFormat.FULL)

    assert _stored_name(player) == "Quenby Marlowfield"


def test_a_change_records_the_formats_and_never_a_name() -> None:
    player = PlayerFactory.create(first_name="Quenby", last_name="Marlowfield")

    _set(player, NameFormat.FIRST_LAST_INITIAL)

    [event] = AuditEvent.objects.all()
    assert event.action == AuditAction.PRIVACY_CHANGE
    assert event.actor_user_id == player.account_id
    assert event.before == {"name_format": "full"}
    assert event.after == {"name_format": "first_last_initial"}
    assert "Marlowfield" not in str([event.before, event.after, event.reason])
    assert "Quenby" not in str([event.before, event.after, event.reason])


def test_an_unchanged_format_records_nothing() -> None:
    player = PlayerFactory.create()

    _set(player, NameFormat.FULL)

    assert not AuditEvent.objects.exists()


def test_a_minor_cannot_switch_to_the_full_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _at(monkeypatch, datetime(2026, 6, 15, 12, tzinfo=UTC))
    player = _create(UserFactory.create(), 2011, 1)

    with pytest.raises(ValidationError) as caught:
        _set(player, NameFormat.FULL)

    assert caught.value.message_dict == {"name_format": [FULL_NAME_FROM_18_MESSAGE]}
    assert caught.value.error_dict["name_format"][0].code == "invalid"
    assert _stored_name(player) == "Quenby M."
    privacy = PlayerPrivacy.objects.get(player=player)
    assert privacy.name_format == NameFormat.FIRST_LAST_INITIAL
    assert not AuditEvent.objects.exists()


def test_a_minor_may_resend_the_stored_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _at(monkeypatch, datetime(2026, 6, 15, 12, tzinfo=UTC))
    player = _create(UserFactory.create(), 2011, 1)

    _set(player, NameFormat.FIRST_LAST_INITIAL)

    assert not AuditEvent.objects.exists()


def test_nothing_changes_at_18_until_the_player_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Born January 2009: 17 in mid 2026, 18 from 31 January 2027 at UTC-12.
    _at(monkeypatch, datetime(2026, 6, 15, 12, tzinfo=UTC))
    player = _create(UserFactory.create(), 2009, 1)
    _at(monkeypatch, datetime(2027, 1, 31, 11, 59, tzinfo=UTC))
    assert is_name_format_locked(player)

    _at(monkeypatch, datetime(2027, 2, 1, 12, tzinfo=UTC))
    assert not is_name_format_locked(player)
    assert _stored_name(player) == "Quenby M."

    _set(player, NameFormat.FULL)

    assert _stored_name(player) == "Quenby Marlowfield"


def test_a_player_with_no_stored_names_keeps_the_display_name() -> None:
    player = PlayerFactory.create(display_name="Orrin Q.")
    Player.objects.filter(pk=player.pk).update(first_name=None, last_name=None)
    player.refresh_from_db()

    _set(player, NameFormat.FIRST_LAST_INITIAL)

    assert _stored_name(player) == "Orrin Q."
    privacy = PlayerPrivacy.objects.get(player=player)
    assert privacy.name_format == NameFormat.FIRST_LAST_INITIAL


def test_a_removed_birth_year_and_month_lock_the_format() -> None:
    player = PlayerFactory.create()
    player.birth_year = None
    player.birth_month = None

    assert is_name_format_locked(player)


def test_an_unknown_format_is_refused() -> None:
    player = PlayerFactory.create()

    with pytest.raises(ValidationError) as caught:
        _set(player, "made-up-format")

    assert list(caught.value.message_dict) == ["name_format"]
    assert "made-up-format" not in str(caught.value)
    assert not AuditEvent.objects.exists()


def test_the_model_default_fails_closed() -> None:
    player = Player.objects.create(
        account=UserFactory.create(),
        display_name="Made-up Player",
        birth_year=1990,
        birth_month=6,
        gender_division="mens",
    )

    privacy = PlayerPrivacy.objects.create(player=player)

    assert privacy.name_format == NameFormat.FIRST_LAST_INITIAL

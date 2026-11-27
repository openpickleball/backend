"""PlayerFactory makes valid Players through create_player, with made-up data."""

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.players.age import age_limit_date
from apps.players.models import GenderDivision, Player, PlayerPrivacy, Visibility
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def test_player_is_valid_and_saved() -> None:
    player = PlayerFactory.create()

    stored = Player.objects.get(pk=player.pk)
    stored.full_clean()
    assert stored.public_code.startswith("P-")


def test_player_has_privacy_settings_at_the_default() -> None:
    player = PlayerFactory.create()

    privacy = PlayerPrivacy.objects.get(player=player)
    for setting in PlayerPrivacy.SETTINGS:
        assert getattr(privacy, setting) == Visibility.SIGNED_IN


def test_each_player_gets_its_own_account() -> None:
    players = PlayerFactory.create_batch(10)

    assert len({player.account_id for player in players}) == 10


def test_values_are_in_range() -> None:
    this_year = age_limit_date().year

    for player in PlayerFactory.create_batch(20):
        assert this_year - 70 <= player.birth_year <= this_year - 19
        assert 1 <= player.birth_month <= 12
        assert player.gender_division in GenderDivision.values
        assert player.display_name
        assert len(player.region) <= 100
        assert player.legal_name == f"{player.first_name} {player.last_name}"


def test_overrides_are_used() -> None:
    account = UserFactory.create()

    player = PlayerFactory.create(account=account, display_name="Made-up Override")

    assert player.account == account
    assert player.display_name == "Made-up Override"


def test_creating_records_no_event() -> None:
    PlayerFactory.create()

    assert not AuditEvent.objects.exists()

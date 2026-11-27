"""The data migration that fills the first and last name (issue #354, ADR 0062).

Each test runs inside the test transaction, which rolls back, so the
schema it migrates back and forward ends at the latest migration again.
Made-up names only.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.utils import timezone

from apps.players.models import NameFormat, Player, PlayerPrivacy
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

BEFORE = [("players", "0011_player_names_and_name_format")]
AFTER = [("players", "0012_fill_player_names")]


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
    """A migration executor that leaves the schema at the latest migration."""
    migrator = MigrationExecutor(connection)
    yield migrator
    migrator.loader.build_graph()
    migrator.migrate(migrator.loader.graph.leaf_nodes())


def _migrate(executor: MigrationExecutor, target: list[tuple[str, str]]) -> None:
    executor.loader.build_graph()
    executor.migrate(target)


def _stored_before_names(
    legal_name: str, display_name: str, **kwargs: object
) -> Player:
    """A Player as it was stored before #354: no names, a typed display name,
    and the model default format."""
    player = PlayerFactory.create(legal_name=legal_name, **kwargs)
    Player.objects.filter(pk=player.pk).update(
        first_name=None, last_name=None, display_name=display_name
    )
    PlayerPrivacy.objects.filter(player=player).update(
        name_format=NameFormat.FIRST_LAST_INITIAL
    )
    return player


def _row(player: Player) -> tuple[str | None, str | None, str, str]:
    stored = Player.objects.get(pk=player.pk)
    privacy = PlayerPrivacy.objects.get(player=player)
    return (
        stored.first_name,
        stored.last_name,
        stored.display_name,
        privacy.name_format,
    )


def test_a_legal_name_that_splits_fills_the_names(
    executor: MigrationExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 6, 15, 12, tzinfo=UTC))
    adult = _stored_before_names("Ysolde Brackenridge", "Yz", birth_year=1980)
    minor = _stored_before_names(
        "Tamsin Van Oort", "Tamsin Van Oort", birth_year=2011, birth_month=1
    )
    _migrate(executor, BEFORE)

    _migrate(executor, AFTER)

    assert _row(adult) == ("Ysolde", "Brackenridge", "Ysolde Brackenridge", "full")
    assert _row(minor) == ("Tamsin", "Van Oort", "Tamsin V.", "first_last_initial")


@pytest.mark.parametrize(
    "legal_name",
    ["", "Cher", "  ", "A" * 62 + " B", "Ysolde " + "B" * 60],
)
def test_a_legal_name_that_does_not_split_keeps_the_display_name(
    executor: MigrationExecutor, legal_name: str
) -> None:
    player = _stored_before_names(legal_name, "Typed Name")
    _migrate(executor, BEFORE)

    _migrate(executor, AFTER)

    assert _row(player) == (None, None, "Typed Name", "full")


def test_no_key_is_needed_when_there_are_no_players(
    executor: MigrationExecutor,
) -> None:
    _migrate(executor, [("players", "0010_moderators_view_players")])

    with override_settings(FIELD_ENCRYPTION_KEYS=[]):
        _migrate(executor, AFTER)
        _migrate(executor, [("players", "0010_moderators_view_players")])

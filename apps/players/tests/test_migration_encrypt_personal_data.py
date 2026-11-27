"""The migrations that encrypt Player personal data (issue #115, ADR 0046).

Each test runs inside the test transaction, which rolls back, so the
schema it migrates back and forward ends at the latest migration again.
Postgres runs schema changes inside a transaction.
"""

from collections.abc import Iterator

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from apps.encryption.keys import MISSING_KEYS_MESSAGE, encrypt_text, is_token
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAIN = [("players", "0003_playerprivacy_rating_visibility_and_more")]
ENCRYPTED = [("players", "0005_encrypt_personal_data")]
LEGAL_NAME = "Qorvath Made-up Legal"
# The columns these migrations encrypt. Later migrations add others
# (ADR 0062), which do not exist at these migrations, so reads load these
# only.
COLUMNS = ("legal_name", "birth_year", "birth_month", "gender_division")
RESTORED_CHECKS = {
    "players_player_birth_month_range",
    "players_player_birth_year_min",
    "players_player_gender_division_valid",
}


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
    """A migration executor that leaves the schema at the latest migration."""
    migrator = MigrationExecutor(connection)
    yield migrator
    migrator.loader.build_graph()
    migrator.migrate(migrator.loader.graph.leaf_nodes())


def _raw() -> list[tuple[object, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT legal_name, birth_year, birth_month, gender_division "
            "FROM players_player ORDER BY id"
        )
        return [tuple(row) for row in cursor.fetchall()]


def _checks() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'players_player'::regclass AND contype = 'c'"
        )
        return {row[0] for row in cursor.fetchall()}


def _immediate() -> None:
    # Deferred foreign key checks would block the schema changes below.
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _migrate(executor: MigrationExecutor, target: list[tuple[str, str]]) -> None:
    executor.loader.build_graph()
    executor.migrate(target)


def test_back_restores_plain_columns_and_forward_encrypts_again(
    executor: MigrationExecutor,
) -> None:
    first = PlayerFactory.create(
        legal_name=LEGAL_NAME, birth_year=1979, birth_month=3, gender_division="mens"
    )
    second = PlayerFactory.create(
        legal_name="", birth_year=1991, birth_month=12, gender_division="womens"
    )
    _immediate()

    _migrate(executor, PLAIN)

    assert _raw() == [(LEGAL_NAME, 1979, 3, "mens"), ("", 1991, 12, "womens")]
    assert _checks() >= RESTORED_CHECKS

    _migrate(executor, ENCRYPTED)

    for row in _raw():
        assert all(isinstance(value, str) and is_token(value) for value in row)
    assert not RESTORED_CHECKS & _checks()
    stored = {
        player.pk: (
            player.legal_name,
            player.birth_year,
            player.birth_month,
            player.gender_division,
        )
        for player in Player.objects.only(*COLUMNS)
    }
    assert stored == {
        first.pk: (LEGAL_NAME, 1979, 3, "mens"),
        second.pk: ("", 1991, 12, "womens"),
    }


def test_forward_leaves_a_value_already_encrypted(
    executor: MigrationExecutor,
) -> None:
    player = PlayerFactory.create(legal_name=LEGAL_NAME, gender_division="mens")
    _immediate()
    # Text columns, values decrypted by the data migration's reverse.
    _migrate(executor, [("players", "0004_alter_player_options_and_more")])
    token = encrypt_text(LEGAL_NAME)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE players_player SET legal_name = %s WHERE id = %s",
            [token, player.pk],
        )

    _migrate(executor, ENCRYPTED)

    [(legal_name, *others)] = _raw()
    assert legal_name == token
    assert all(isinstance(value, str) and is_token(value) for value in others)
    stored = Player.objects.only(*COLUMNS).get(pk=player.pk)
    assert (stored.legal_name, stored.gender_division) == (LEGAL_NAME, "mens")


def test_no_key_is_needed_when_there_are_no_players(
    executor: MigrationExecutor,
) -> None:
    _migrate(executor, PLAIN)

    with override_settings(FIELD_ENCRYPTION_KEYS=[]):
        _migrate(executor, ENCRYPTED)
        _migrate(executor, PLAIN)


def test_players_without_a_key_stop_the_migration_with_fixed_text(
    executor: MigrationExecutor,
) -> None:
    PlayerFactory.create(legal_name=LEGAL_NAME)
    _immediate()
    _migrate(executor, PLAIN)

    with (
        override_settings(FIELD_ENCRYPTION_KEYS=[]),
        pytest.raises(ImproperlyConfigured) as caught,
    ):
        _migrate(executor, ENCRYPTED)

    assert str(caught.value) == MISSING_KEYS_MESSAGE

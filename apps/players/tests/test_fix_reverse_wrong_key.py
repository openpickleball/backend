"""The Player data migration's reverse refuses a value it cannot decrypt
(issue #115, ADR 0046).

With FIELD_ENCRYPTION_KEYS missing the key a row was written with, the
reverse must stop with fixed text naming the model and row id, never
the value, and change nothing, instead of leaving the token for the
column cast to choke on.
"""

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAIN = [("players", "0003_playerprivacy_rating_visibility_and_more")]
LEGAL_NAME = "Quillon Made-up Legal"


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
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


def test_reverse_with_another_key_raises_and_changes_nothing(
    executor: MigrationExecutor,
) -> None:
    player = PlayerFactory.create(legal_name=LEGAL_NAME)
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    before = _raw()

    executor.loader.build_graph()
    with (
        override_settings(FIELD_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]),
        pytest.raises(RuntimeError) as caught,
    ):
        executor.migrate(PLAIN)

    message = str(caught.value)
    assert f"players.Player row {player.pk}" in message
    assert LEGAL_NAME not in message
    assert "gAAAA" not in message
    assert _raw() == before
